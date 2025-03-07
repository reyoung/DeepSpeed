import os
import torch
import tqdm
import deepspeed
import deepspeed.ops.transformer as transformer_inference
from deepspeed.ops.transformer.inference.diffusers_attention import DeepSpeedDiffusersAttention
from deepspeed.ops.transformer.inference.diffusers_transformer_block import DeepSpeedDiffusersTransformerBlock
from deepspeed.ops.transformer.inference.diffusers_2d_transformer import Diffusers2DTransformerConfig
from deepspeed.accelerator import get_accelerator
from .replace_policy import HFGPT2LayerPolicy
from .replace_policy import replace_policies, generic_policies

from deepspeed import comm as dist
from torch import nn

from .layers import LinearAllreduce, LinearLayer
from .load_checkpoint import load_model_with_checkpoint
import time

from .utils import policy_to_ds_container


class ReplaceWithTensorSlicing:
    def __init__(self, mp_group=None, mp_size=1, out_dim=1, in_dim=0):
        if mp_group is not None:
            self.gpu_index = dist.get_rank(group=mp_group)
        else:
            self.gpu_index = 0
        self.out_dim = out_dim
        self.in_dim = in_dim
        self.mp_size = mp_size

    def merge_assert(self, dim1, dim2):
        assert dim1 > dim2, \
            'Merging tensors is not allowed here! Please use deepspeed load_checkpoint\
            for merging your checkpoints before replacing the transformer layer with\
            inference-kernels'

    def qkv_copy(self, dst, src):
        if src is None:
            return src
        src_shape = src.shape
        dst_shape = dst.shape

        if self.out_dim == 0:
            src_split = torch.split(src.data,
                                    src_shape[self.out_dim] // self.mp_size,
                                    dim=0)
        else:
            src_split = torch.split(src.data, src.shape[-1] // 3, dim=-1)
        if (len(src_shape) == 2 and len(dst_shape) == 2):
            if src_shape[self.out_dim] == dst_shape[self.out_dim]:
                return torch.nn.parameter.Parameter(src)
            if self.out_dim == 1:
                self.merge_assert(src_shape[self.out_dim], dst_shape[self.out_dim])
                qkv_size = dst_shape[self.out_dim] // 3
                qkv_split = [
                    torch.split(src_s,
                                qkv_size,
                                dim=self.out_dim) for src_s in src_split
                ]
                weight_split = [
                    torch.cat([qkv_s[i] for qkv_s in qkv_split],
                              axis=self.out_dim) for i in range(len(qkv_split[0]))
                ]
                dst.data.copy_(weight_split[self.gpu_index].to(
                    get_accelerator().current_device_name()).contiguous())
            else:
                dst.data.copy_(src_split[self.gpu_index].to(
                    get_accelerator().current_device_name()).contiguous())
        else:
            if src_shape[0] == dst_shape[0]:
                return torch.nn.parameter.Parameter(src)
            if self.out_dim == 1:
                qkv_size = dst_shape[0] // 3
                qkv_split = [torch.split(src_s, qkv_size, dim=0) for src_s in src_split]
                bias_split = [
                    torch.cat([qkv_s[i] for qkv_s in qkv_split],
                              axis=0) for i in range(len(qkv_split[0]))
                ]
                dst.data.copy_(bias_split[self.gpu_index].to(
                    get_accelerator().current_device_name()).contiguous())
            else:
                dst.data.copy_(src_split[self.gpu_index].to(
                    get_accelerator().current_device_name()).contiguous())

        return torch.nn.parameter.Parameter(dst)

    def copy(self, dst, src):
        if src is None:
            return src
        src_shape = src.shape
        dst_shape = dst.shape
        if (len(src_shape) == 2 and len(dst_shape) == 2):

            if src_shape[0] == dst_shape[0] and src_shape[1] == dst_shape[1]:
                dst.data.copy_(src)
            else:
                if src_shape[self.in_dim] != dst_shape[self.in_dim]:
                    self.merge_assert(src_shape[self.in_dim], dst_shape[self.in_dim])
                    weight_split = torch.split(
                        src,
                        dst_shape[self.in_dim],
                        dim=self.in_dim)[self.gpu_index].to(
                            get_accelerator().current_device_name()).contiguous()
                else:
                    self.merge_assert(src_shape[self.out_dim], dst_shape[self.out_dim])
                    weight_split = torch.split(
                        src.data,
                        dst_shape[self.out_dim],
                        dim=self.out_dim)[self.gpu_index].to(
                            get_accelerator().current_device_name()).contiguous()
                dst.data.copy_(weight_split.contiguous())
        else:
            if src_shape[0] == dst_shape[0]:
                dst.data.copy_(src)
            else:
                bias_split = torch.split(
                    src.data,
                    dst_shape[-1])[self.gpu_index].to(
                        get_accelerator().current_device_name()).contiguous()
                dst.data.copy_(bias_split)
        dst = torch.nn.parameter.Parameter(dst, requires_grad=False)
        if hasattr(src, 'scale'):
            dst.scale = src.scale
        return dst


def get_transformer_name(replaced_module):
    from .containers import supported_models
    from torch.nn import ModuleList
    transformer_name = ''
    for n, c in replaced_module.named_children():
        if c.__class__ in supported_models:
            transformer_name += n + '.'
            for name, child in c.named_children():
                if child.__class__ is ModuleList:
                    transformer_name += name
                    break
            break
    return transformer_name


class GroupQuantizer:
    def __init__(self, q_int8=True, group_size=1, num_bits=8):
        self.group_size = group_size
        self.num_bits = num_bits
        self.q_int8 = q_int8
        # TODO(jeff): need to check w. Reza on why this is needed when changing tp size w. bloom
        self.num_groups = 32

    def quantize(self, inputs, qkv=True, count=1, parallel_dim=0):
        if not self.q_int8 or not qkv:
            inputs = torch.nn.Parameter(inputs, requires_grad=False)
            inputs.scale = torch.empty(1)
            return inputs
        q_range = 2**self.num_bits
        num_groups = inputs.shape[0] // self.group_size
        inputs = inputs.to(get_accelerator().current_device_name())
        input_flat = inputs.reshape(num_groups, -1).contiguous()
        input_min = torch.min(input_flat, dim=1, keepdim=True)[0].float()
        input_max = torch.max(input_flat, dim=1, keepdim=True)[0].float()
        scale = torch.max(input_min.abs(), input_max.abs()) * 2.0 / (q_range)
        input_flat = (input_flat / scale).round().clamp(-q_range // 2, q_range // 2 - 1)
        inputs_q = input_flat.reshape(inputs.shape).to(torch.int8).contiguous()
        out = torch.nn.Parameter(inputs_q, requires_grad=False)
        #print(inputs.shape)
        inputs_split = inputs.split(inputs.shape[parallel_dim] // 2, dim=parallel_dim)
        input_flat = [
            inputs_split[i].reshape(num_groups,
                                    -1).contiguous() for i in range(2)
        ]
        input_min = [
            torch.min(input_flat[i],
                      dim=1,
                      keepdim=True)[0].float() for i in range(2)
        ]
        input_max = [
            torch.max(input_flat[i],
                      dim=1,
                      keepdim=True)[0].float() for i in range(2)
        ]
        scale1 = [
            (torch.max(input_min[i].abs(),
                       input_max[i].abs()) * 2.0 / (q_range)).squeeze().unsqueeze(0)
            for i in range(2)
        ]

        out.scale = torch.cat([scale.squeeze().unsqueeze(0),
                               scale1[0],
                               scale1[1]],
                              dim=0).reshape(num_groups,
                                             -1).contiguous()
        return out


def _module_match(module):
    for policy in generic_policies:
        policy = policy()
        if policy.match(module):
            return policy
    return None


def generic_injection(module, fp16=False, enable_cuda_graph=True):
    def replace_attn(child, policy):
        policy_attn = policy.attention(child)
        if policy_attn is None:
            return child
        if len(policy_attn) == 5:
            qkvw, attn_ow, attn_ob, hidden_size, heads = policy_attn
        else:
            qw, kw, vw, attn_ow, attn_ob, hidden_size, heads = policy_attn

        config = transformer_inference.DeepSpeedInferenceConfig(
            hidden_size=hidden_size,
            heads=heads,
            fp16=fp16,
            triangular_masking=False,
            max_out_tokens=4096,
        )
        attn_module = DeepSpeedDiffusersAttention(config)

        def transpose(data):
            data = data.contiguous()
            data.reshape(-1).copy_(data.transpose(-1, -2).contiguous().reshape(-1))
            data = data.reshape(data.shape[-1], data.shape[-2])
            data.to(get_accelerator().current_device_name())
            return data

        if len(policy_attn) == 5:
            attn_module.attn_qkvw.data = transpose(qkvw.data)
        else:
            attn_module.attn_qkvw = None
            attn_module.attn_qw.data = transpose(qw.data)
            attn_module.attn_kw.data = transpose(kw.data)
            attn_module.attn_vw.data = transpose(vw.data)

        attn_module.attn_qkvb = None
        attn_module.attn_ow.data = transpose(attn_ow.data)
        attn_module.attn_ob.data.copy_(
            attn_ob.data.to(get_accelerator().current_device_name()))
        return attn_module

    def replace_attn_block(child, policy):
        config = Diffusers2DTransformerConfig()
        return DeepSpeedDiffusersTransformerBlock(child, config)

    if isinstance(module, torch.nn.Module):
        pass
    else:
        if fp16 is False:
            raise ValueError("Generic injection only supported with FP16")

        try:
            import diffusers
            cross_attention = diffusers.models.attention.CrossAttention
            attention_block = diffusers.models.attention.BasicTransformerBlock
            new_policies = {
                cross_attention: replace_attn,
                attention_block: replace_attn_block,
            }
        except ImportError:
            new_policies = {}

        #replace_transformer_layer(None,
        #                          module.text_encoder,
        #                          training=False,
        #                          replace_with_kernel_inject=True,
        #                          triangular_masking=True,
        #                          max_out_tokens=8192)
        from ..model_implementations.transformers.clip_encoder import DSClipEncoder
        cg_encoder = DSClipEncoder(module.text_encoder,
                                   enable_cuda_graph=enable_cuda_graph)
        setattr(module, 'text_encoder', cg_encoder)
        for name in module.__dict__.keys():
            sub_module = getattr(module, name)
            policy = _module_match(sub_module)

            if policy is not None:

                def _replace_module(module, policy):
                    for name, child in module.named_children():
                        _replace_module(child, policy)
                        if child.__class__ in new_policies:
                            replaced_module = new_policies[child.__class__](child,
                                                                            policy)
                            setattr(module, name, replaced_module)

                _replace_module(sub_module, policy)
                new_module = policy.apply(sub_module,
                                          enable_cuda_graph=enable_cuda_graph)
                print(f"**** found and replaced {name} w. {type(new_module)}")
                setattr(module, name, new_module)


container_g = None


def replace_transformer_layer(orig_layer_impl,
                              model,
                              checkpoint_dict,
                              config,
                              model_config):
    """ Replace bert-style transformer layers with DeepSpeed's transformer layer
    Arguments:
        orig_layer_impl (torch.nn.Module): the original transformer layer implementation to look for,
            e.g., transformers.modeling_bert.BertLayer.
        model (torch.nn.Module): user's nn.module representing their model
        checkpoint_dict: Dictionary for checkpoint passed from the Inference Engine
        config: top-level DS Inference config defined in inference/config.py
        model_config: HuggingFace model config passed from the inference/engine.py
    Returns:
        Updated nn.module with replaced transformer layers
    """
    # defining globals as internally defined functions inherit these everywhere
    fp16 = (config.dtype == torch.float16 or config.dtype == torch.int8)
    quantize = (config.dtype == torch.int8)
    # todo: Refactor later. In future, let's minimize the style used above and use config.** instead

    linear_layer_setting = None
    '''
        linear_layer_setting (tuple of modules) [Optional]: shows which two classes are used for linear layers and embedding layers
    '''
    micro_batch_size = -1
    seed = -1
    local_rank = -1

    mp_replace = ReplaceWithTensorSlicing(
        mp_group=config.tensor_parallel.tp_group,
        mp_size=config.tensor_parallel.tp_size)  #, out_dim=0, in_dim=1)

    def replace_with_policy(child,
                            policy_cls,
                            triangular_masking,
                            inference=False,
                            layer_id=0):
        policy = policy_cls(child, inference=inference)
        if not policy.cuda_graph_supported:
            # policy says cuda graph is not supported raise an error if set
            assert not config.enable_cuda_graph, "cuda graph is not supported with this model, please disable"

        from deepspeed.moe.layer import MoE
        moe = False
        if hasattr(child, 'mlp') and isinstance(child.mlp, MoE):
            num_experts = child.mlp.num_experts
            moe = True

        # 1. Create a model-specific container object using the policy object.
        _container = policy_to_ds_container(policy=policy,
                                            config=config,
                                            model_config=model_config,
                                            layer_id=layer_id,
                                            child=child)
        _container.set_dtype(fp16)
        _container.set_moe(moe)

        # 2. Set the tensor parallelism config
        _container.set_tensor_parallel_config(config.tensor_parallel.tp_size,
                                              config.tensor_parallel.tp_group)

        # 3. Initialize tensors
        _container.initialize_tensors()

        # 4. deal with data types -- needs refactor to use dtype instead of fp16
        if fp16:
            _container.convert_to_required_dtype(dtype=torch.half)

        # 5. Set the quantization config
        quantizer = GroupQuantizer(q_int8=quantize)
        _container.set_quantization_config(quantize, quantizer)

        # 6. create a DS Inference config object
        _container.create_ds_model_config()

        # 7. use the config and create the module
        _container.create_module()

        # 8. transpose the weights and bias if needed
        _container.transpose()

        # 9. deal with tensor parallelism.
        _container.apply_tensor_parallelism(mp_replace)

        # 10. copy the tensors from the model-specific container to the new module
        _container.copy_data_to_new_module()

        # 11. set globals for generic checkpoint loading
        global container_g

        if container_g is None:
            container_g = _container

        return _container.module

    def replace_wo_policy(module, all_reduce_linears):
        mp_size = config.tensor_parallel.tp_size
        mp_group = config.tensor_parallel.tp_group

        def _replace(child, name, conv_linear_layer):
            mp_replace = ReplaceWithTensorSlicing(mp_group=mp_group)
            weight_shape = child.weight.shape
            if name in all_reduce_linears:
                new_weight = torch.empty((
                    weight_shape[1] if conv_linear_layer else weight_shape[0],
                    (weight_shape[0] if conv_linear_layer else weight_shape[1]) //
                    mp_size,
                ),
                                         device=child.weight.device,
                                         dtype=child.weight.dtype)
                if conv_linear_layer:
                    child.weight.data = child.weight.data.transpose(-1, -2).contiguous()
                data = mp_replace.copy(new_weight, child.weight.data)
                new_bias = torch.empty((weight_shape[0]),
                                       device=child.weight.device,
                                       dtype=child.weight.dtype)
                if child.bias is not None:
                    new_bias.data.copy_(child.bias.data)
                return LinearAllreduce(data, child.bias if child.bias is None else \
                            torch.nn.parameter.Parameter(new_bias.to(get_accelerator().current_device_name())), mp_group)
            else:
                new_weight = torch.empty((
                    (weight_shape[1] if conv_linear_layer else weight_shape[0]) //
                    mp_size,
                    weight_shape[0] // mp_size if conv_linear_layer else weight_shape[1],
                ),
                                         device=child.weight.device,
                                         dtype=child.weight.dtype)
                if conv_linear_layer:
                    child.weight.data = child.weight.data.transpose(-1, -2).contiguous()
                data = mp_replace.copy(new_weight, child.weight.data)

                new_bias = torch.empty((weight_shape[0] // mp_size),
                                       device=child.weight.device,
                                       dtype=child.weight.dtype)
                bias_data = None if child.bias is None else mp_replace.copy(
                    new_bias,
                    child.bias.data).to(get_accelerator().current_device_name())
                return LinearLayer(weight=data.to(
                    get_accelerator().current_device_name()),
                                   bias=bias_data)

        def _slice_embedding(child, name, conv_linear_layer):
            mp_replace = ReplaceWithTensorSlicing(mp_group=mp_group)
            new_weight = torch.empty((child.weight.shape[0],
                                      child.weight.shape[1] // mp_size),
                                     device=child.weight.device,
                                     dtype=child.weight.dtype)
            data = mp_replace.copy(new_weight,
                                   child.weight.ds_tensor.data if hasattr(child.weight, 'ds_tensor') else \
                                   child.weight.data)
            new_embedding = nn.Embedding(child.weight.shape[0],
                                         child.weight.shape[1] // mp_size)
            new_embedding.weight.data.copy_(data)
            return new_embedding

        def update_mp_params(child):
            if hasattr(child, 'n_heads'):
                child.n_heads = child.n_heads // mp_size
            if hasattr(child, 'inner_dim'):
                child.inner_dim = child.inner_dim // mp_size
            if hasattr(child, 'num_heads'):
                child.num_heads = child.num_heads // mp_size
            if hasattr(child, 'num_attention_heads'):
                child.num_attention_heads = child.num_attention_heads // mp_size
            if hasattr(child, 'num_attn_heads'):
                child.num_attn_heads = child.num_attn_heads // mp_size
            if hasattr(child, 'all_head_size'):
                child.all_head_size = child.all_head_size // mp_size
            if hasattr(child, 'embed_dim'):
                child.embed_dim = child.embed_dim // mp_size
            if hasattr(child, 'hidden_size'):
                child.hidden_size = child.hidden_size // mp_size

        conv_linear_layer = False
        if linear_layer_setting is not None:
            linear_policies = {linear_layer_setting[0]: _replace}
            if len(linear_layer_setting) == 2:
                linear_policies.update({linear_layer_setting[1]: _slice_embedding})
        else:
            if orig_layer_impl is HFGPT2LayerPolicy._orig_layer_class:
                try:
                    import transformers
                    conv_linear_layer = True
                    linear_policies = {transformers.model_utils.Conv1D: _replace}
                except ImportError:
                    linear_policies = {nn.Linear: _replace}
            else:
                linear_policies = {nn.Linear: _replace, nn.Embedding: _slice_embedding}

        def _replace_module(r_module, prev_name=''):
            for name, child in r_module.named_children():
                if child.__class__ in linear_policies:
                    setattr(
                        r_module,
                        name,
                        linear_policies[child.__class__](child,
                                                         prev_name + '.' + name,
                                                         conv_linear_layer))
                else:
                    update_mp_params(child)
                    _replace_module(child, name)
            return r_module

        return _replace_module(module)

    def replace_fn(child, _policy, layer_id=0):
        training = False  # todo: refactor this part to go in the config
        if training:
            # copy relevant state from child -> new module
            new_module = replace_with_policy(child, _policy, config.triangular_masking)

        else:
            # copy relevant state from child -> new module
            if config.replace_with_kernel_inject:
                new_module = replace_with_policy(child,
                                                 _policy,
                                                 config.triangular_masking,
                                                 inference=True,
                                                 layer_id=layer_id)
            else:
                new_module = replace_wo_policy(child, _policy)

        return new_module

    replaced_module = replace_module(model=model,
                                     orig_class=orig_layer_impl,
                                     replace_fn=replace_fn,
                                     _replace_policy=config.injection_policy_tuple)

    quantizer = GroupQuantizer(q_int8=quantize)
    world_size = dist.get_world_size() if dist.is_initialized() else 1
    rank = dist.get_rank() if dist.is_initialized() else 0
    if checkpoint_dict is not None:
        assert container_g.ckpt_load_enabled, \
               f"Meta Tensor checkpoint loading not supported in {container_g.__class__.__name__} container"
        start_time = time.time()
        checkpoint = checkpoint_dict['checkpoints']
        ckpt_list = checkpoint["tp"] if type(checkpoint) is dict else checkpoint
        ckpt_type = checkpoint_dict.get('parallelization', 'pp')
        ckpt_mp_size = checkpoint_dict.get('tp_size', len(ckpt_list))
        ckpt_mp_size = checkpoint_dict.get('mp_size', ckpt_mp_size)
        base_dir1 = checkpoint_dict.get('base_dir', config.base_dir)

        if ckpt_type == 'pp' and type(checkpoint) is list:
            pbar = tqdm.tqdm(total=len(checkpoint),
                             desc=f"Loading {len(checkpoint)} checkpoint shards")

            for i in range(len(checkpoint)):
                sd = [
                    torch.load(os.path.join(base_dir1,
                                            checkpoint[i]),
                               map_location='cpu')
                ]
                load_model_with_checkpoint(
                    replaced_module,
                    sd,
                    mp_replace,
                    ckpt_type,
                    quantizer,
                    param_names=container_g.get_param_names(),
                    transformer_config=container_g.ds_model_config,
                    megatron_v2=container_g.megatron_v2)
                pbar.update(1)
        else:
            import gc
            num_checkpoints = len(ckpt_list) // ckpt_mp_size
            tp_split_size = (world_size / ckpt_mp_size)
            sd_offset = int(rank / tp_split_size)
            sd_count = int((rank + max(1, tp_split_size)) / tp_split_size) - sd_offset
            pbar = tqdm.tqdm(total=num_checkpoints,
                             desc=f"Loading {num_checkpoints} checkpoint shards")
            for i in range(num_checkpoints):
                pbar.update(1)
                ckpt_index = i * ckpt_mp_size + sd_offset
                ckpt_files = [
                    os.path.join(base_dir1,
                                 ckpt_list[ckpt_index +
                                           j]) if base_dir1 else ckpt_list[ckpt_index +
                                                                           j]
                    for j in range(sd_count)
                ]
                sds = [
                    torch.load(ckpt_file,
                               map_location='cpu') for ckpt_file in ckpt_files
                ]
                load_model_with_checkpoint(
                    replaced_module,
                    sds,
                    mp_replace,
                    ckpt_type,
                    quantizer,
                    int(rank % tp_split_size),
                    param_names=container_g.get_param_names(),
                    transformer_config=container_g.ds_model_config,
                    megatron_v2=container_g.megatron_v2)
                sds = [None for _ in sds]
                gc.collect()

            if "non_tp" in checkpoint:
                pbar = tqdm.tqdm(
                    total=len(checkpoint["non_tp"]),
                    desc=f"Loading {len(checkpoint['non_tp'])} checkpoint shards")

                for i in range(len(checkpoint["non_tp"])):
                    pbar.update(1)
                    ckpt_file = os.path.join(base_dir1,
                                             checkpoint["non_tp"][i]
                                             ) if base_dir1 else checkpoint["non_tp"][i]
                    sds = [torch.load(ckpt_file, map_location='cpu')]
                    load_model_with_checkpoint(
                        replaced_module,
                        sds,
                        mp_replace,
                        ckpt_type,
                        quantizer,
                        int(rank % tp_split_size),
                        param_names=container_g.get_param_names(),
                        transformer_config=container_g.ds_model_config,
                        megatron_v2=container_g.megatron_v2)
                    sds = [None for _ in sds]
                    gc.collect()
        print(f"checkpoint loading time at rank {rank}: {time.time()-start_time} sec")

    if config.save_mp_checkpoint_path is not None:
        from collections import OrderedDict
        import json
        num_partitions = 8

        if checkpoint_dict is None:
            ckpt_name = "ds_model"
            try:
                from transformers.models.bloom.modeling_bloom import BloomForCausalLM
                if isinstance(model, BloomForCausalLM):
                    ckpt_name = "bloom"
            except ImportError:
                ckpt_name = "ds_model"
        else:
            ckpt_name = checkpoint_dict['type']
        if dist.is_initialized():
            dist.barrier()
        transformer_name = get_transformer_name(replaced_module)
        non_tp_ckpt_name = f'non-tp.pt'
        ckpt_files = [non_tp_ckpt_name]
        os.makedirs(config.save_mp_checkpoint_path, exist_ok=True)

        if not dist.is_initialized() or dist.get_rank() == 0:
            print("Saving tp-sharded checkpoints")
            torch.save(
                OrderedDict({
                    k: v
                    for k,
                    v in dict(replaced_module.state_dict()).items()
                    if transformer_name not in k
                }),
                f'{config.save_mp_checkpoint_path}/{non_tp_ckpt_name}')
            ckpt_config = json.dumps({
                'type':
                ckpt_name,
                'base_dir':
                f'{config.save_mp_checkpoint_path}',
                'checkpoints': {
                    "non_tp":
                    ckpt_files,
                    "tp": [
                        f'tp_{r:0>2d}_{m:0>2d}.pt' for m in range(num_partitions)
                        for r in range(world_size)
                    ]
                },
                'version':
                1.0,
                'parallelization':
                'tp',
                'tp_size':
                world_size,
                'dtype':
                'int8' if quantize else ('float16' if fp16 else 'float32')
            })
            with open(f"{config.save_mp_checkpoint_path}/ds_inference_config.json",
                      "w") as cfg:
                cfg.write(ckpt_config)

        rep_sd = replaced_module.state_dict()
        for n, p in replaced_module.named_parameters():
            if hasattr(p, 'scale'):
                rep_sd[n] = [p, p.scale]
        keys = list(rep_sd.keys())
        partition_size = (len(keys) // num_partitions + 1)
        for m in range(num_partitions):
            torch.save(
                OrderedDict({
                    k: [rep_sd[k],
                        rep_sd[k].scale] if hasattr(rep_sd[k],
                                                    'scale') else rep_sd[k]
                    for k in keys[m * partition_size:(m + 1) * partition_size]
                    if transformer_name in k
                }),
                f'{config.save_mp_checkpoint_path}/tp_{rank:0>2d}_{m:0>2d}.pt')

    return replaced_module


def revert_transformer_layer(orig_layer_impl, model, config, preln=False):
    """ Revert DeepSpeed's transformer layer back to original bert-style transformer layer
    Arguments:
        orig_layer_impl (torch.nn.Module): the original transformer layer implementation that was replaced,
            e.g., transformers.modeling_bert.BertLayer.
        model (torch.nn.Module): user's nn.module representing their model
        config (dict): model config containing hidden size, attention heads, etc.
    Returns:
        Updated nn.module with original bert-style transformer layers
    """
    def replace_fn(child, _replace_policy, layer_id):
        #from turing.nvidia_modelingpreln import BertLayer
        orig_module = orig_layer_impl(config)

        # copy relevant state from child -> original module
        qkvw = child.attn_qkvw.data
        qkvb = child.attn_qkvb.data

        qw, kw, vw = torch.chunk(qkvw, 3, axis=0)
        qb, kb, vb = torch.chunk(qkvb, 3, axis=0)

        orig_module.attention.self.query.weight.data = qw
        orig_module.attention.self.query.bias.data = qb
        orig_module.attention.self.key.weight.data = kw
        orig_module.attention.self.key.bias.data = kb
        orig_module.attention.self.value.weight.data = vw
        orig_module.attention.self.value.bias.data = vb

        orig_module.attention.output.dense.weight.data = child.attn_ow.data
        orig_module.attention.output.dense.bias.data = child.attn_ob.data

        attn_ln_w = child.attn_nw.data
        attn_ln_b = child.attn_nb.data
        if preln:
            orig_module.PostAttentionLayerNorm.weight.data = attn_ln_w
            orig_module.PostAttentionLayerNorm.bias.data = attn_ln_b
        else:
            orig_module.attention.output.LayerNorm.weight.data = attn_ln_w
            orig_module.attention.output.LayerNorm.bias.data = attn_ln_b

        inter_ff_w = child.inter_w.data
        inter_ff_b = child.inter_b.data
        if preln:
            orig_module.intermediate.dense_act.weight.data = inter_ff_w
            orig_module.intermediate.dense_act.bias.data = inter_ff_b
        else:
            orig_module.intermediate.dense.weight.data = inter_ff_w
            orig_module.intermediate.dense.bias.data = inter_ff_b

        orig_module.output.dense.weight.data = child.output_w.data
        orig_module.output.dense.bias.data = child.output_b.data

        transformer_ln_w = child.norm_w.data
        transformer_ln_b = child.norm_b.data
        if preln:
            orig_module.PreAttentionLayerNorm.weight.data = transformer_ln_w
            orig_module.PreAttentionLayerNorm.bias.data = transformer_ln_b
        else:
            orig_module.output.LayerNorm.weight.data = transformer_ln_w
            orig_module.output.LayerNorm.bias.data = transformer_ln_b
        return orig_module

    return replace_module(model=model,
                          orig_class=deepspeed.DeepSpeedTransformerLayer,
                          replace_fn=replace_fn,
                          _replace_policy=None)


def replace_module(model, orig_class, replace_fn, _replace_policy):
    """ Scan the model for instances of ``orig_clas:`` to replace using ``replace_fn``.
    Arguments:
        model (torch.nn.Module): the model to augment
        orig_class (torch.nn.Module): the module to search for
        replace_fn (method): a method to convert instances of ``orig_class`` to the
                             desired type and return a new instance.
    Returns:
        A modified ``model``.
    """
    policy = {}
    if orig_class is not None:
        policy.update({orig_class: (replace_fn, _replace_policy)})
    else:
        for plcy in replace_policies:
            # instantiate a throw-away policy in order to populate the _orig_layer_class
            _ = plcy(None)
            if isinstance(plcy._orig_layer_class, list):
                for orig_layer_class in plcy._orig_layer_class:
                    policy.update({orig_layer_class: (replace_fn, plcy)})
            elif plcy._orig_layer_class is not None:
                policy.update({plcy._orig_layer_class: (replace_fn, plcy)})
    assert len(policy.items()) > 0,\
        "No default policy found! Please specify your policy injection_policy (like {BertLayer:HFBEertLayerPolicy})." +\
        "You can find some samples here: https://github.com/microsoft/DeepSpeed/blob/master/deepspeed/module_inject/replace_policy.py"

    replaced_module, _ = _replace_module(model, policy)
    return replaced_module


from ..pipe import PipelineModule


def _replace_module(model, policies, layer_id=0):
    """ Traverse model's children recursively and apply any transformations in ``policies``.
    Arguments:
        model (torch.nn.Module): model to augment
        policies (dict): Mapping of source class to replacement function.
    Returns:
        Modified ``model``.
    """
    for name, child in model.named_children():
        if child.__class__ in policies:
            replaced_module = policies[child.__class__][0](child,
                                                           policies[child.__class__][-1],
                                                           layer_id)
            setattr(model, name, replaced_module)
            if isinstance(model, PipelineModule):
                assert hasattr(model, 'forward_funcs'),\
                    "we require pipe-module to have the list of fwd_functions"
                model.forward_funcs[model.fwd_map[name]] = replaced_module
            layer_id += 1
        else:
            _, layer_id = _replace_module(child, policies, layer_id=layer_id)

    # Add the reset_cache func to the model, so that it can be called in the beginning of text-generation.
    model.reset_cache = transformer_inference.DeepSpeedTransformerInference.reset_cache
    return model, layer_id
