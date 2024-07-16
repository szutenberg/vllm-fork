###############################################################################
# Copyright (C) 2024 Habana Labs, Ltd. an Intel Company
###############################################################################

import time
from enum import IntEnum
from dataclasses import dataclass, replace
from typing import List, NamedTuple, Optional, Set, Tuple, Dict

import collections
import gc
import os
import sys
import math
import itertools
import operator
import torch
import habana_frameworks.torch as htorch

from vllm.attention import (AttentionMetadata, AttentionMetadataPerStage,
                            get_attn_backend)
from vllm.config import (DeviceConfig, LoadConfig, CacheConfig, LoRAConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig, VisionLanguageConfig)
from vllm.distributed import broadcast_tensor_dict
from vllm.distributed.parallel_state import get_cpu_world_group
from vllm.logger import init_logger
from vllm.lora.layers import LoRAMapping
from vllm.lora.request import LoRARequest
from vllm.lora.worker_manager import LRUCacheWorkerLoRAManager
from vllm.model_executor import SamplingMetadata
from vllm.model_executor.model_loader import get_model
from vllm.sampling_params import SamplingParams
from vllm.sequence import SamplerOutput, SequenceData, SequenceGroupMetadata
from vllm.utils import (HabanaMemoryProfiler, is_pin_memory_available,
                        make_tensor_with_pad, format_bytes)

from .profiler import Profiler

logger = init_logger(__name__)

_TYPE_CACHE = {}
_PAD_SLOT_ID = 0
LORA_WARMUP_RANK = 8


def subtuple(obj: object, typename: str, to_copy: List[str], to_override: Dict[str, object] = {}):
    if obj is None:
        return None
    fields = set(to_copy) | set(to_override.keys())
    values = {f: to_override.get(f, getattr(obj, f)) for f in fields}
    if typename not in _TYPE_CACHE:
        _TYPE_CACHE[typename] = collections.namedtuple(typename, ' '.join(fields))
    return _TYPE_CACHE[typename](**values)


def setup_profiler():
    DEVICE='hpu'
    STEPS=3
    activities = [torch.profiler.ProfilerActivity.CPU]
    activities.extend([torch.profiler.ProfilerActivity.HPU] if DEVICE == 'hpu' else [])
    wait = 0
    active = 1
    warmup = STEPS - active

    schedule = torch.profiler.schedule(wait=wait, warmup=warmup, active=active, repeat=1)
    profiler = torch.profiler.profile(
        schedule=schedule,
        activities=activities,
        on_trace_ready=torch.profiler.tensorboard_trace_handler('.', use_gzip=True),
        record_shapes=False,
        with_stack=True)
    return profiler


def pt_profiler(schedule):
    DEVICE = 'hpu'
    activities = [torch.profiler.ProfilerActivity.CPU]
    activities.extend([torch.profiler.ProfilerActivity.HPU] if DEVICE == 'hpu' else [])
    #from habana_frameworks.torch.activity_profiler import DebugActivity
    #debug_activities=[DebugActivity.BRIDGE_FUNCTION_CALLS]

    profiler = torch.profiler.profile(
        schedule=schedule,
        activities=activities,
        #debug_activities=debug_activities,
        on_trace_ready=torch.profiler.tensorboard_trace_handler('.', use_gzip=True),
        record_shapes=False,
        with_stack=True)
    return profiler


def hltv_profiler(schedule):
    pt_tools_path = os.environ.get('PT_TOOLS_PATH', None)
    assert pt_tools_path is not None, "Need to specify PT_TOOLS_PATH to use hltv profiling method"
    sys.path.append(pt_tools_path)
    from topologies import SynapseProfilerApi, TraceType
    api = SynapseProfilerApi()
    class SynapseProfiler:
        def check(self):
            if schedule(self.cur_step) == torch.profiler.ProfilerAction.RECORD_AND_SAVE:
                api.profiler_start(TraceType.TraceAll, 0)
        def start(self):
            self.cur_step = 0
            self.check()
        def step(self):
            self.cur_step = self.cur_step + 1
            self.check()
        def stop(self):
            api.profiler_stop(TraceType.TraceAll, 0)
            api.profiler_get_trace_json(TraceType.TraceAll, 0)
    return SynapseProfiler()


def setup_profiler():
    prof_wait = 0
    prof_warmup = 2
    prof_active = 1
    prof_type = os.environ.get('VLLM_PT_PROFILE_METHOD', 'pt')
    assert prof_type in ['pt', 'hltv']
    method = pt_profiler if prof_type == 'pt' else hltv_profiler
    schedule = torch.profiler.schedule(wait=prof_wait, warmup=prof_warmup, active=prof_active, repeat=1)
    return method(schedule)


# Read bucketing configuration from env variables
# phase is either 'prompt' or 'decode'
# dim is either 'bs' or 'block'
# param is either 'min', 'step' or 'max'
# example env variable: VLLM_DECODE_BS_BUCKET_STEP=128
def read_bucket_settings(phase: str, dim: str, **defaults: Dict):
    params = ['min', 'step', 'max']
    env_vars = [f'VLLM_{phase}_{dim}_BUCKET_{p}'.upper() for p in params]
    defaults = [defaults[p] for p in params]
    values = [int(os.environ.get(e, d)) for e, d in zip(env_vars, defaults)]
    for e, v, d in zip(env_vars, values, defaults):
        logger.info(f'{e}={v} (default:{d})')
    return values


def warmup_range(config: Tuple[int, int, int]):
    bmin, bstep, bmax = config
    base = itertools.repeat(2)
    ramp_up = itertools.accumulate(base, func=operator.mul, initial=bmin)
    ramp_up = itertools.takewhile(lambda x: x < bstep and x <= bmax, ramp_up)
    stable = range(max(bmin, bstep), bmax + 1, bstep)
    return list(ramp_up) + list(stable)


def generate_prompt_buckets(bs_bucket_config, seq_bucket_config):
    buckets = itertools.product(warmup_range(bs_bucket_config), warmup_range(seq_bucket_config))
    return list(sorted(buckets, key=lambda b: (b[0] * b[1], b[1], b[0])))


def generate_decode_buckets(bs_bucket_config, blocks_bucket_config, max_blocks):
    buckets = []
    for bs in warmup_range(bs_bucket_config):
        for blocks in warmup_range(blocks_bucket_config):
            if blocks < bs:
                continue
            if blocks > max_blocks:
                break
            buckets.append((bs, blocks))
    return list(sorted(buckets, key=lambda b: (b[0] * b[1], b[1], b[0])))


def next_pow2(value: int, base: int):
    res = base
    while value > 1:
        value = (value + 1) // 2
        res *= 2
    return res


def round_up(value: int, k: int):
    return (value + k - 1) // k * k


def find_bucket(value: int, config: Tuple[int, int, int]):
    bmin, bstep, _ = config
    next_step = round_up(value, bstep)
    next_pow = next_pow2(value, bmin)
    return max(bmin, min(next_step, next_pow))


def align_workers(value, op):
    group = get_cpu_world_group()
    world_size = torch.distributed.get_world_size()
    if world_size <= 1:
        return value
    value_t = torch.tensor(value, device='cpu')
    torch.distributed.all_reduce(value_t, op=op, group=group)
    return value_t.item()


def pad_list(l, k, v):
    target_len = round_up(len(l), k)
    padding = target_len - len(l)
    return l + [v] * padding


class HpuModelAdapter():
    def __init__(self, model, block_size):
        self.model = model
        self.block_size = block_size

    def _set_attn_bias(self, metadata, batch_size, seq_len, device, dtype):
        seq_lens_t = metadata.seq_lens_tensor
        len_mask = (torch.arange(0, seq_len, device=device, dtype=torch.int32)
                    .view(1, seq_len)
                    .ge(seq_lens_t.unsqueeze(-1))
                    .view(batch_size, 1, 1, seq_len))
        causal_mask = torch.triu(
            torch.ones((batch_size, 1, seq_len, seq_len), device=device, dtype=torch.bool),
            diagonal=1
        )
        mask = causal_mask.logical_or(len_mask)
        attn_bias = (torch.zeros_like(mask, dtype=dtype)
                        .masked_fill_(mask, -math.inf))
        return metadata._replace(attn_bias=attn_bias)

    def _set_block_mapping(self, metadata, batch_size, device, dtype):
        mask = torch.arange(0, self.block_size, device=device, dtype=torch.int32).unsqueeze(0)
        mask = mask >= metadata.block_usage.unsqueeze(-1)
        attn_bias = (torch.zeros_like(mask, dtype=dtype)
                        .masked_fill_(mask, -math.inf))
        block_mapping = torch.nn.functional.one_hot(metadata.block_mapping, num_classes=batch_size).to(dtype)
        metadata = metadata._replace(block_mapping=block_mapping, attn_bias=attn_bias)
        return metadata

    def _update_metadata(self, attn_metadata, batch_size, seq_len, device, dtype):
        if (meta := attn_metadata.prefill_metadata) is not None:
            return attn_metadata._replace(prefill_metadata=self._set_attn_bias(meta, batch_size, seq_len, device, dtype))
        if (meta := attn_metadata.decode_metadata) is not None:
            return attn_metadata._replace(decode_metadata=self._set_block_mapping(meta, batch_size, device, dtype))
        return attn_metadata

    def forward(self, *args, **kwargs):
        kwargs = kwargs.copy()
        selected_token_indices = kwargs.pop('selected_token_indices')
        if 'warmup_mode' in kwargs:
            kwargs.pop('warmup_mode')
        input_ids = kwargs['input_ids']
        kwargs['attn_metadata'] = self._update_metadata(kwargs['attn_metadata'], input_ids.size(0), input_ids.size(1), input_ids.device, torch.bfloat16)
        hidden_states = self.model(*args, **kwargs)
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
        hidden_states = hidden_states.index_select(0, selected_token_indices)
        return hidden_states

    def compute_logits(self, *args, **kwargs):
        return self.model.compute_logits(*args, **kwargs)

    def sample(self, *args, **kwargs):
        return self.model.sample(*args, **kwargs)


class PreparePromptMetadata(NamedTuple):
    input_tokens: List[int]
    input_positions: List[int]
    attn_metadata: Optional[AttentionMetadataPerStage]
    seq_lens: List[int]
    query_lens: List[int]
    lora_index_mapping: List[int]
    lora_prompt_mapping: List[int]
    lora_requests: Set[LoRARequest]
    multi_modal_input: Optional[torch.Tensor]
    slot_mapping: List[int]

    @classmethod
    def empty(cls):
        return PreparePromptMetadata(
            input_tokens=[],
            input_positions=[],
            attn_metadata=None,
            seq_lens=[],
            query_lens=[],
            lora_index_mapping=[],
            lora_prompt_mapping=[],
            lora_requests=set(),
            multi_modal_input=None,
            slot_mapping=[],
        )


class PrepareDecodeMetadata(NamedTuple):
    input_tokens: List[int]
    input_positions: List[int]
    attn_metadata: Optional[AttentionMetadata]
    lora_index_mapping: List[int]
    lora_prompt_mapping: List[int]
    lora_requests: Set[LoRARequest]
    slot_mapping: List[int]

    @classmethod
    def empty(cls):
        return PrepareDecodeMetadata(
            input_tokens=[],
            input_positions=[],
            attn_metadata=None,
            lora_index_mapping=[],
            lora_prompt_mapping=[],
            lora_requests=set(),
            slot_mapping=[],
        )


# How batches are constructed.
class BatchType(IntEnum):
    # Every batch is prefill.
    PREFILL = 0
    # Every batch is decode.
    DECODE = 1
    # Batch is a mixture of prefill and decode.
    MIXED = 2


class HabanaModelRunner:

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
        load_config: LoadConfig,
        cache_config: CacheConfig,
        lora_config: Optional[LoRAConfig],
        kv_cache_dtype: Optional[str] = "auto",
        is_driver_worker: bool = False,
        vision_language_config: Optional[VisionLanguageConfig] = None,
    ):
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.lora_config = lora_config
        self.load_config = load_config
        self.cache_config = cache_config
        self.is_driver_worker = is_driver_worker
        self.profiler = Profiler()

        self.sliding_window = (model_config.get_sliding_window()
                               if model_config is not None else None)
        self.device_config = (device_config
                              if device_config is not None else DeviceConfig())

        self.device = self.device_config.device
        self.enforce_eager = self.model_config.enforce_eager
        self.max_num_seqs = self.scheduler_config.max_num_seqs
        self.max_model_len = self.scheduler_config.max_model_len
        self.max_num_batched_tokens = self.scheduler_config.max_num_batched_tokens
        self.block_size = cache_config.block_size

        self.pin_memory = is_pin_memory_available()
        self.kv_cache_dtype = kv_cache_dtype
        self.vision_language_config = vision_language_config

        self.attn_backend = get_attn_backend(
            self.model_config.dtype if model_config is not None else None)

        # Lazy initialization
        self.lora_manager: LRUCacheWorkerLoRAManager = None
        self.model: torch.nn.Module = None

        # Profiler stats
        self.profiler_counter_helper = HabanaProfilerCounterHelper()
        self.seen_configs = set()

        self._setup_buckets()
        self.skip_warmup = os.environ.get('VLLM_SKIP_WARMUP', 'false').lower() == 'true'

    def load_model(self) -> None:
        with HabanaMemoryProfiler() as m:
            with HabanaMemoryProfiler() as m_getmodel:
                self.model = get_model(
                    model_config=self.model_config,
                    device_config=self.device_config,
                    load_config=self.load_config,
                    lora_config=self.lora_config,
                    vision_language_config=self.vision_language_config,
                    parallel_config=self.parallel_config,
                    scheduler_config=self.scheduler_config,
                )
            logger.info(f"Pre-loading model weights on {next(self.model.parameters()).device} took {m_getmodel.get_summary_string()}")

            import habana_frameworks.torch.core as htcore
            if self.model_config.quantization == 'hqt':
                logger.info("Preparing model with HQT..")
                with HabanaMemoryProfiler() as m_hqt:
                    import habana_quantization_toolkit
                    habana_quantization_toolkit.prep_model(self.model)
                    htcore.hpu_initialize(self.model, mark_only_scales_as_const=True)
                logger.info(f"Preparing model with HQT took {m_hqt.get_summary_string()}")
            else:
                self.model = self.model.to("hpu")
                htcore.mark_step()
            torch.hpu.synchronize()

            if self.scheduler_config.enable_delayed_sampling:
                self.model.sampler.include_gpu_probs_tensor = True
                self.model.sampler.sample_token_positions_only = True

            self.model = _maybe_wrap_in_hpu_graph(HpuModelAdapter(self.model, self.block_size))
        self.model_memory_usage = m.consumed_device_memory
        logger.info(f"Loading model weights took in total {m.get_summary_string()}")

        if self.lora_config:
            assert hasattr(self.model, "supported_lora_modules"
                           ) and self.model.supported_lora_modules, (
                               "Model does not support LoRA")
            assert hasattr(
                self.model,
                "embedding_modules"), "Model does not have embedding_modules"
            assert hasattr(self.model, "embedding_padding_modules"
                           ), "Model does not have embedding_padding_modules"
            self.lora_manager = LRUCacheWorkerLoRAManager(
                self.scheduler_config.max_num_seqs,
                self.scheduler_config.max_num_batched_tokens, self.vocab_size,
                self.lora_config, self.device, self.model.embedding_modules,
                self.model.embedding_padding_modules)
            self.model = self.lora_manager.create_lora_manager(self.model)

    def _use_graphs(self, batch_size, seq_len, is_prompt):
        if self.enforce_eager:
            return False
        if self.skip_warmup:
            return True
        return (batch_size, seq_len, is_prompt) in self.graphed_buckets

    def _setup_buckets(self) -> None:
        align_bs = lambda x: min(self.max_num_seqs, x)
        blocks_step = 128
        max_prompt_seq = 1024
        max_decode_seq = 2048
        self.prompt_bs_bucket_cfg = read_bucket_settings('prompt', 'bs', min=1, step=align_bs(32), max=align_bs(64))
        self.decode_bs_bucket_cfg = read_bucket_settings('decode', 'bs', min=align_bs(32), step=align_bs(32), max=self.max_num_seqs)
        self.prompt_seq_bucket_cfg = read_bucket_settings('prompt', 'seq', min=self.block_size, step=self.block_size, max=max_prompt_seq)
        self.decode_block_bucket_cfg = read_bucket_settings('decode', 'block', min=blocks_step, step=blocks_step, max=max(blocks_step, self.max_num_seqs * max_decode_seq // self.block_size))
        self.graphed_buckets = set()
        logger.info(f"Prompt bucket config (min, step, max_warmup) bs:{self.prompt_bs_bucket_cfg}, seq:{self.prompt_seq_bucket_cfg}")
        logger.info(f"Decode bucket config (min, step, max_warmup) bs:{self.decode_bs_bucket_cfg}, seq:{self.decode_block_bucket_cfg}")

    def _prepare_prompt(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> PreparePromptMetadata:
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []
        lora_index_mapping: List[List[int]] = []
        lora_prompt_mapping: List[List[int]] = []
        lora_requests: Set[LoRARequest] = set()

        seq_lens: List[int] = []
        context_lens: List[int] = []
        query_lens: List[int] = []
        prefix_block_tables: List[List[int]] = []
        multi_modal_input_list: List[torch.Tensor] = []

        if len(seq_group_metadata_list) == 0:
            return PreparePromptMetadata.empty()

        for seq_group_metadata in seq_group_metadata_list:
            assert seq_group_metadata.is_prompt
            seq_ids = list(seq_group_metadata.seq_data.keys())
            assert len(seq_ids) == 1
            seq_id = seq_ids[0]

            computed_block_nums = seq_group_metadata.computed_block_nums
            if (self.scheduler_config is not None
                    and self.scheduler_config.chunked_prefill_enabled
                    and not (computed_block_nums is None
                             or computed_block_nums == [])):
                raise RuntimeError(
                    "chunked prefill cannot be used with prefix caching "
                    "now.")

            token_chunk_size = seq_group_metadata.token_chunk_size
            seq_data = seq_group_metadata.seq_data[seq_id]
            context_len = seq_data.get_num_computed_tokens()
            # We should use get_len here because in case of preemption
            # it contains output tokens.
            seq_len = min(seq_data.get_len(), context_len + token_chunk_size)
            prompt_tokens = seq_data.get_token_ids()[context_len:seq_len]
            seq_lens.append(seq_len)

            # NOTE: This only works for oooooooxxx style attention.
            if computed_block_nums is not None and len(
                    computed_block_nums) > 0 and self.sliding_window is None:
                # Prefix is not supported with sliding_window
                context_len = len(computed_block_nums) * self.block_size
                prompt_tokens = prompt_tokens[context_len:]
                prefix_block_tables.append(computed_block_nums)
            elif self.scheduler_config.chunked_prefill_enabled:
                if seq_group_metadata.block_tables is not None:
                    # Prefill has chunked before.
                    block_table = seq_group_metadata.block_tables[seq_id]
                    prefix_block_tables.append(block_table)
                else:
                    # The first prefill.
                    prefix_block_tables.append([])
            else:
                prefix_block_tables.append([])
                # Right now, prefill start is always 0. However, this
                # assumption can be changed once chunked prefill is introduced.
                assert context_len == 0

            # actual prompt lens
            context_lens.append(context_len)
            query_lens.append(seq_len - context_len)

            input_tokens.append(prompt_tokens)
            # NOTE(woosuk): Here we assume that the first token in the prompt
            # is always the first token in the sequence.
            input_positions.append(list(range(context_len, seq_len)))
            lora_id = seq_group_metadata.lora_int_id

            if lora_id > 0:
                lora_requests.add(seq_group_metadata.lora_request)

            lora_index_mapping += [lora_id] * (seq_len - context_len)
            lora_prompt_mapping.append(
                [lora_id] *
                (seq_len - context_len
                 if seq_group_metadata.sampling_params.prompt_logprobs else 1))

            if seq_group_metadata.multi_modal_data:
                multi_modal_input_list.append(
                    seq_group_metadata.multi_modal_data.data)

            if seq_group_metadata.block_tables is None:
                # During memory profiling, the block tables are not initialized
                # yet. In this case, we just use a dummy slot mapping.
                slot_mapping.append([_PAD_SLOT_ID] * seq_len)
                continue

            # Compute the slot mapping.
            slot_mapping.append([])
            block_table = seq_group_metadata.block_tables[seq_id]

            # Mask the [0, start_idx) tokens of the prompt with _PAD_SLOT_ID,
            # where start_idx is max(0, seq_len - sliding_window).
            # For example, if the prompt len is 10, sliding window is 8, and
            # block size is 4, the first two tokens are masked and the slot
            # mapping will be [-1, -1, 2, 3, 4, 5, 6, 7, 0, 1].
            start_idx = 0
            if self.sliding_window is not None:
                assert context_len == 0, (
                    "Prefix caching is currently not supported with "
                    "sliding window attention")
                start_idx = max(0, seq_len - self.sliding_window)
            for i in range(context_len, seq_len):
                if i < start_idx:
                    slot_mapping[-1].append(_PAD_SLOT_ID)
                    continue

                block_number = block_table[i // self.block_size]
                block_offset = i % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping[-1].append(slot)

        max_query_len = max(query_lens)
        assert max_query_len > 0

        if multi_modal_input_list:
            assert self.vision_language_config, (
                "Multi-modal inputs are only supported by "
                "vision language models.")
            multi_modal_input = torch.cat(multi_modal_input_list,
                                          dim=0).to(self.device)
        else:
            multi_modal_input = None

        max_prompt_len = max(find_bucket(max(seq_lens), self.prompt_seq_bucket_cfg), self.block_size)

        input_tokens = make_tensor_with_pad(input_tokens,
                                            max_prompt_len,
                                            pad=0,
                                            dtype=torch.long,
                                            device=self.device)

        input_positions = make_tensor_with_pad(input_positions,
                                               max_prompt_len,
                                               pad=0,
                                               dtype=torch.long,
                                               device=self.device)

        slot_mapping = make_tensor_with_pad(slot_mapping,
                                            max_prompt_len,
                                            pad=_PAD_SLOT_ID,
                                            dtype=torch.long,
                                            device=self.device)

        seq_lens_tensor = torch.tensor(seq_lens,
                                       dtype=torch.long,
                                       device=self.device)

        attn_metadata = self.attn_backend.make_metadata(
            block_list=None,
            block_mapping=None,
            block_usage=None,
            attn_bias=None,
            seq_lens_tensor=seq_lens_tensor,
        )
        return PreparePromptMetadata(
            input_tokens=input_tokens,
            input_positions=input_positions,
            attn_metadata=attn_metadata,
            seq_lens=seq_lens,
            query_lens=query_lens,
            lora_index_mapping=lora_index_mapping,
            lora_prompt_mapping=lora_prompt_mapping,
            lora_requests=lora_requests,
            multi_modal_input=multi_modal_input,
            slot_mapping=slot_mapping,
        )

    def _prepare_decode(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> PrepareDecodeMetadata:
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []
        seq_lens: List[int] = []
        block_tables: List[List[int]] = []
        lora_index_mapping: List[int] = []
        lora_prompt_mapping: List[int] = []
        lora_requests: Set[LoRARequest] = set()

        if len(seq_group_metadata_list) == 0:
            return PrepareDecodeMetadata.empty()

        for seq_group_metadata in seq_group_metadata_list:
            assert not seq_group_metadata.is_prompt
            assert seq_group_metadata.token_chunk_size == 1

            seq_ids = list(seq_group_metadata.seq_data.keys())
            #lora_id = seq_group_metadata.lora_int_id

            #if lora_id > 0:
            #    lora_requests.add(seq_group_metadata.lora_request)

            for seq_id in seq_ids:
                seq_data = seq_group_metadata.seq_data[seq_id]
                generation_token = seq_data.get_last_token_id()
                input_tokens.append(generation_token)

                seq_len = seq_data.get_len()
                position = (seq_data.get_num_computed_tokens()
                            if self.scheduler_config.enable_delayed_sampling else (seq_len - 1))
                input_positions.append([position])

                seq_len = seq_len if self.sliding_window is None else min(
                    seq_len, self.sliding_window)
                seq_lens.append(seq_len)

                block_table = seq_group_metadata.block_tables[seq_id]
                block_number = block_table[position // self.block_size]
                block_offset = position % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping.append([slot])
                #lora_index_mapping.append(lora_id)
                #lora_prompt_mapping.append(lora_id)

                if self.sliding_window is not None:
                    sliding_window_blocks = (self.sliding_window //
                                             self.block_size)
                    block_table = block_table[-sliding_window_blocks:]
                block_tables.append(block_table)

        input_tokens = torch.tensor(input_tokens,
                                    dtype=torch.long,
                                    device=self.device).unsqueeze(-1)
        input_positions = torch.tensor(input_positions,
                                    dtype=torch.long,
                                    device=self.device)

        blocks_used = [len(bt) for bt in block_tables]
        block_list = list(itertools.chain(*block_tables))
        block_mapping = [[i] * bu for i, bu in enumerate(blocks_used)]
        block_mapping = list(itertools.chain(*block_mapping))

        last_block = [sl % self.block_size + 1 for sl in itertools.chain(*slot_mapping)]
        block_usage = [[self.block_size] * (bu - 1) + [lb] for bu, lb in zip(blocks_used, last_block)]
        block_usage = list(itertools.chain(*block_usage))

        block_bucket_size = self.decode_block_bucket_cfg[1]
        block_list = pad_list(block_list, block_bucket_size, _PAD_SLOT_ID)
        block_mapping = pad_list(block_mapping, block_bucket_size, 0)
        block_usage = pad_list(block_usage, block_bucket_size, 0)

        block_list = torch.tensor(block_list, dtype=torch.int, device=self.device)
        block_mapping = torch.tensor(block_mapping, dtype=torch.int, device=self.device)
        block_usage = torch.tensor(block_usage, dtype=torch.bfloat16, device=self.device)

        slot_mapping = torch.tensor(slot_mapping,
                                    dtype=torch.long,
                                    device=self.device)

        attn_metadata = self.attn_backend.make_metadata(
            block_list=block_list,
            block_mapping=block_mapping,
            block_usage=block_usage,
            attn_bias=None,
            seq_lens_tensor=None,
        )
        return PrepareDecodeMetadata(
            input_tokens=input_tokens,
            input_positions=input_positions,
            attn_metadata=attn_metadata,
            lora_index_mapping=lora_index_mapping,
            lora_prompt_mapping=lora_prompt_mapping,
            lora_requests=lora_requests,
            slot_mapping=slot_mapping,
        )

    def prepare_input_tensors(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, AttentionMetadata, SamplingMetadata,
               Set[LoRARequest], LoRAMapping, torch.Tensor]:
        if self.is_driver_worker:
            prefill_reqs = []
            decode_reqs = []
            for seq_group_meta in seq_group_metadata_list:
                if seq_group_meta.is_prompt:
                    prefill_reqs.append(seq_group_meta)
                else:
                    decode_reqs.append(seq_group_meta)

            # Prepare input tensors.
            (
                input_tokens,
                input_positions,
                prefill_attn_metadata,
                seq_lens,
                query_lens,
                lora_index_mapping,
                lora_prompt_mapping,
                lora_requests,
                multi_modal_input,
                slot_mapping,
            ) = self._prepare_prompt(prefill_reqs)
            (
                decode_input_tokens,
                decode_input_positions,
                decode_attn_metadata,
                decode_lora_index_mapping,
                decode_lora_prompt_mapping,
                decode_lora_requests,
                decode_slot_mapping,
            ) = self._prepare_decode(decode_reqs)
            sampling_metadata = SamplingMetadata.prepare(
                seq_group_metadata_list, seq_lens, query_lens, self.device,
                self.pin_memory)

            if not self.scheduler_config.chunked_prefill_enabled:
                assert (len(prefill_reqs) and len(decode_reqs)) == 0

            num_prefills = len(seq_lens)
            num_prefill_tokens = len(input_tokens)
            num_decode_tokens = len(decode_input_tokens)

            # NOTE(kzawora): Here we diverge from GPU code - we don't support mixed batches, so we either use decode or prefill inputs, without coalescing.
            assert (num_prefills == 0 and num_decode_tokens > 0) or (num_prefills > 0 and num_decode_tokens == 0), "HPU does not support mixed batches!"
            if num_decode_tokens > 0:
                input_tokens = decode_input_tokens
                input_positions = decode_input_positions
                slot_mapping = decode_slot_mapping
                lora_index_mapping = decode_lora_index_mapping
                lora_prompt_mapping = decode_lora_prompt_mapping
                lora_requests = decode_lora_requests

            # FIXME: We need to adjust selected_token_indices to accomodate for padding
            max_len = input_tokens.size(1)
            paddings = [max_len - s for s in seq_lens]
            paddings = [0] + paddings[:-1]
            paddings = list(itertools.accumulate(paddings))
            paddings = torch.tensor(paddings, dtype=sampling_metadata.selected_token_indices.dtype, device=sampling_metadata.selected_token_indices.device)
            sampling_metadata.selected_token_indices.add_(paddings)

            if self.lora_config:
                lora_mapping = LoRAMapping(
                    lora_index_mapping,
                    lora_prompt_mapping,
                )
            else:
                lora_mapping = None

            if (prefill_attn_metadata is not None
                    and decode_attn_metadata is not None):
                batch_type = BatchType.MIXED
                raise NotImplementedError("Mixed batch is not supported on HPU")
            elif prefill_attn_metadata is not None:
                batch_type = BatchType.PREFILL
            else:
                batch_type = BatchType.DECODE

            metadata_dict = {
                "input_tokens": input_tokens,
                "input_positions": input_positions,
                "selected_token_indices":
                sampling_metadata.selected_token_indices,
                "lora_requests": lora_requests,
                "lora_mapping": lora_mapping,
                "multi_modal_input": multi_modal_input,
                "num_prefill_tokens": num_prefill_tokens,
                "num_decode_tokens": num_decode_tokens,
                "slot_mapping": slot_mapping,
                "num_prefills": num_prefills,
                "batch_type": batch_type,
            }
            if prefill_attn_metadata is not None:
                metadata_dict.update(prefill_attn_metadata.asdict_zerocopy())
            else:
                assert decode_attn_metadata is not None
                metadata_dict.update(decode_attn_metadata.asdict_zerocopy())
            broadcast_tensor_dict(metadata_dict, src=0)

            # Broadcast decode attn metadata for mixed batch type.
            # The additional broadcast costs 300us overhead on 4 A10 GPUs.
            # We can potentially reduce the overhead by coelescing tensors.
            if batch_type == BatchType.MIXED:
                assert decode_attn_metadata is not None
                metadata_dict = decode_attn_metadata.asdict_zerocopy()
                broadcast_tensor_dict(metadata_dict, src=0)
        else:
            metadata_dict = broadcast_tensor_dict(src=0)
            input_tokens = metadata_dict.pop("input_tokens")
            input_positions = metadata_dict.pop("input_positions")
            slot_mapping = metadata_dict.pop("slot_mapping")
            num_prefills = metadata_dict.pop("num_prefills")
            selected_token_indices = metadata_dict.pop(
                "selected_token_indices")
            lora_mapping = metadata_dict.pop("lora_mapping")
            lora_requests = metadata_dict.pop("lora_requests")
            multi_modal_input = metadata_dict.pop("multi_modal_input")
            num_prefill_tokens = metadata_dict.pop("num_prefill_tokens")
            num_decode_tokens = metadata_dict.pop("num_decode_tokens")
            batch_type = metadata_dict.pop("batch_type")

            # Create an attention metadata.
            prefill_attn_metadata = None
            decode_attn_metadata = None
            if batch_type == BatchType.PREFILL or batch_type == BatchType.MIXED:
                prefill_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)
            else:
                decode_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)
            sampling_metadata = SamplingMetadata(
                seq_groups=None,
                selected_token_indices=selected_token_indices,
                categorized_sample_indices=None,
                num_prompts=0,
            )

            # if it is a mixed batch, decode attn_metadata is broadcasted
            # separately.
            if batch_type == BatchType.MIXED:
                metadata_dict = broadcast_tensor_dict(src=0)
                decode_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)

        attn_metadata = AttentionMetadata(
            num_prefills=num_prefills,
            slot_mapping=slot_mapping,
            num_prefill_tokens=num_prefill_tokens,
            num_decode_tokens=num_decode_tokens,
            prefill_metadata=prefill_attn_metadata,
            decode_metadata=decode_attn_metadata,
            kv_cache_dtype=self.kv_cache_dtype,
        )

        return (input_tokens, input_positions, attn_metadata,
                sampling_metadata, lora_requests, lora_mapping,
                multi_modal_input)

    def _seq_len(self, attn_metadata):
        if attn_metadata.prefill_metadata:
            return attn_metadata.slot_mapping.size(1)
        else:
            return attn_metadata.decode_metadata.block_list.numel()

    def trim_attn_metadata(self, metadata: AttentionMetadata) -> object:
        prefill_metadata = subtuple(metadata.prefill_metadata,
                                    "TrimmedPrefillMetadata",
                                    ['attn_bias', 'seq_lens_tensor'])
        decode_metadata = subtuple(metadata.decode_metadata,
                                    "TrimmedDecodeMetadata",
                                    ['attn_bias', 'block_list', 'block_mapping', 'block_usage'])
        return subtuple(metadata,
                        'TrimmedMetadata',
                        ['slot_mapping',
                         'kv_cache_dtype'],
                        {'prefill_metadata': prefill_metadata,
                         'decode_metadata': decode_metadata})

    def finish_measurements(self):
        import habana_quantization_toolkit
        habana_quantization_toolkit.finish_measurements(self.model.model)

    def _check_config(self, batch_size, seq_len, is_prompt, warmup_mode):
        cfg = (batch_size, seq_len, is_prompt)
        seen = cfg in self.seen_configs
        self.seen_configs.add(cfg)
        if not seen and not warmup_mode:
            phase = 'prompt' if is_prompt else 'decode'
            logger.warning(f'Configuration: ({phase}, {batch_size}, {seq_len}) was not warmed-up!')

    @torch.inference_mode()
    def execute_model(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]],
        kv_caches: List[torch.Tensor],
        warmup_mode=False,
    ) -> Optional[SamplerOutput]:
        if self.is_driver_worker:
            event_start = self.profiler.get_timestamp_us()
            is_prompt = seq_group_metadata_list[0].is_prompt
            base_event_name = 'prompt' if is_prompt else 'decode'
            self.profiler.start('internal', base_event_name)

            real_batch_size = len(seq_group_metadata_list)
            bucket_cfg = self.prompt_bs_bucket_cfg if is_prompt else self.decode_bs_bucket_cfg
            batch_size_padded = find_bucket(real_batch_size, bucket_cfg)
            batch_size_padding = batch_size_padded - real_batch_size
            seq_group_metadata_list = seq_group_metadata_list.copy()
            seq_group_metadata_list.extend(seq_group_metadata_list[0] for _ in range(batch_size_padding))
        with self.profiler.record_event('internal', 'prepare_input_tensors'):
            (input_tokens, input_positions, attn_metadata, sampling_metadata,
            lora_requests, lora_mapping, multi_modal_input
            ) = self.prepare_input_tensors(seq_group_metadata_list)
            is_prompt = attn_metadata.prefill_metadata is not None

        if self.lora_config:
            self.set_active_loras(lora_requests, lora_mapping)

        batch_size = input_tokens.size(0)
        seq_len = self._seq_len(attn_metadata)
        use_graphs = self._use_graphs(batch_size, seq_len, is_prompt)
        self._check_config(batch_size, seq_len, is_prompt, warmup_mode)
        execute_model_kwargs = {
            "input_ids": input_tokens,
            "positions": input_positions,
            "kv_caches": kv_caches,
            "attn_metadata": self.trim_attn_metadata(attn_metadata),
        }
        if self.vision_language_config:
            execute_model_kwargs.update({"image_input": multi_modal_input})
        if htorch.utils.internal.is_lazy():
            execute_model_kwargs.update({"bypass_hpu_graphs":not use_graphs})
        htorch.core.mark_step()
        # Sample the next token based on previous logits if any.
        if self.scheduler_config.enable_delayed_sampling and not is_prompt:
            logits_ids_list = []
            logits_tensor = None
            logits_tensor_list = []
            for seq_group_metadata in seq_group_metadata_list:
                assert len(seq_group_metadata.seq_data) == 1
                for seq_data in seq_group_metadata.seq_data.values():
                    if seq_data.prev_logits is not None:
                        if logits_tensor is None:
                            logits_tensor = seq_data.prev_logits
                        if seq_data.prev_logits is logits_tensor:
                            # accumulate row ids from the same tensor
                            logits_ids_list.append(seq_data.prev_logits_idx)
                        else:
                            # new logits tensor, gather all previously collected rows
                            logits_tensor_list.append(logits_tensor[torch.tensor(logits_ids_list, device=seq_data.prev_logits.device)])
                            logits_ids_list = [seq_data.prev_logits_idx]
                            logits_tensor = seq_data.prev_logits
                    else:
                        # warmup only, TODO add a check
                        logits_tensor_list.append(torch.zeros([1, 32000], dtype=torch.float, device="hpu"))
            if logits_tensor is not None:
                logits_tensor_list.append(logits_tensor[torch.tensor(logits_ids_list, device=seq_data.prev_logits.device)])

            prev_logits = torch.cat(logits_tensor_list, dim=0)

            with self.profiler.record_event('internal', f'sample_{"prompt" if is_prompt else "decode"}_bs{batch_size}_seq{seq_len}'):
                output = self.model.sample(
                    logits=prev_logits,
                    sampling_metadata=sampling_metadata,
                )

            execute_model_kwargs["input_ids"] = output.sampled_token_ids
            htorch.core.mark_step()

        if self.is_driver_worker:
            model_event_name = f"model_{'prompt' if is_prompt else 'decode'}_bs{batch_size}_seq{seq_len}_graphs{'T' if use_graphs else 'F'}"
        else:
            model_event_name = 'model_executable'
        with self.profiler.record_event('internal', model_event_name):
            hidden_states = self.model.forward(**execute_model_kwargs, selected_token_indices=sampling_metadata.selected_token_indices)

        if self.scheduler_config.enable_delayed_sampling:
            if not is_prompt:
                htorch.core.mark_step()
                # Only after dispatching next model.forward() read and update the previous token ids to return
                sampled_token_ids = output.sampled_token_ids.tolist()
                for seq_group_output in output.outputs[:real_batch_size]:
                    for sample in seq_group_output.samples:
                        sample.output_token = sampled_token_ids[sample.output_token][0]
                output = output
            else:
                # For prompts compose empty output
                from vllm.sequence import (Logprob, SamplerOutput, SequenceGroupOutput, SequenceOutput)
                sampler_output = []
                for seq_group in sampling_metadata.seq_groups:
                    seq_ids = seq_group.seq_ids
                    next_token_id, parent_id = -1, 0
                    seq_outputs = []
                    seq_outputs.append(
                        SequenceOutput(seq_ids[parent_id], next_token_id, {-1: Logprob(0.0)}))
                    sampler_output.append(
                        SequenceGroupOutput(seq_outputs, None))

                sampled_token_probs, logprobs_tensor, sampled_token_ids = (None, None, None)
                output = SamplerOutput(
                    outputs=sampler_output,
                    sampled_token_probs=sampled_token_probs,
                    sampled_token_ids=sampled_token_ids,
                    logprobs=logprobs_tensor,
                )

            output.outputs = output.outputs[:real_batch_size]
            htorch.core.mark_step()

        # Compute the logits.
        with self.profiler.record_event('internal', f'compute_logits_{"prompt" if is_prompt else "decode"}_bs{batch_size}_seq{seq_len}'):
            sampling_metadata.selected_token_indices = None
            logits = self.model.compute_logits(hidden_states, sampling_metadata)

        if self.scheduler_config.enable_delayed_sampling:
            for idx, seq_group_metadata in enumerate(seq_group_metadata_list):
                assert len(seq_group_metadata.seq_data) == 1
                for seq_data in seq_group_metadata.seq_data.values():
                    seq_data.prev_logits = logits
                    seq_data.prev_logits_idx = idx

        htorch.core.mark_step()

        # Only perform sampling in the driver worker.
        if not self.is_driver_worker:
            return None

        # Sample the next token.
        if not self.scheduler_config.enable_delayed_sampling:
            with self.profiler.record_event('internal', f'sample_{"prompt" if is_prompt else "decode"}_bs{batch_size}_seq{seq_len}'):
                output = self.model.sample(
                    logits=logits,
                    sampling_metadata=sampling_metadata,
                )
            output.outputs = output.outputs[:real_batch_size]
        htorch.core.mark_step()

        if self.is_driver_worker and self.profiler.enabled:
            # Stop recording 'execute_model' event
            self.profiler.end()
            event_end = self.profiler.get_timestamp_us()
            counters = self.profiler_counter_helper.get_counter_dict(
                cache_config=self.cache_config, 
                duration=event_end-event_start, 
                seq_len=seq_len, 
                batch_size_padded=batch_size_padded, 
                real_batch_size=real_batch_size, 
                seq_group_metadata_list=seq_group_metadata_list, 
                is_prompt=is_prompt)
            self.profiler.record_counter(event_start, counters)

        return output

    def create_dummy_seq_group_metadata(self, group_id, seq_len, is_prompt):
        sampling_params = SamplingParams(temperature=0)
        num_blocks = math.ceil(seq_len / self.block_size)
        if is_prompt:
            input_len = seq_len
            output_len = 0
            block_tables = None
        else:
            input_len = seq_len - 1
            output_len = 1
            block_tables = {group_id: [0] * num_blocks}
        prompt_token_ids = [0] * input_len
        output_token_ids = [1] * output_len
        seq_data = SequenceData(prompt_token_ids)
        seq_data.output_token_ids = output_token_ids
        return SequenceGroupMetadata(
            request_id=str(group_id),
            is_prompt=(output_len == 0),
            seq_data={group_id: seq_data},
            sampling_params=sampling_params,
            block_tables=block_tables,
        )

    def profile_run(self) -> None:
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        kv_caches = [None] * num_layers
        max_batch_size = self.prompt_bs_bucket_cfg[-1]
        max_seq_len = self.prompt_seq_bucket_cfg[-1]

        self.warmup_scenario(max_batch_size, max_seq_len, True, kv_caches)

    def warmup_scenario(self, batch_size, seq_len, is_prompt, kv_caches, profile = False) -> None:
        use_graphs = self._use_graphs(batch_size, seq_len, is_prompt)
        scenario_name = f"warmup_{'prompt' if is_prompt else 'decode'}_bs{batch_size}_seq{seq_len}_graphs{'T' if use_graphs else 'F'}"
        self.profiler.start('internal', scenario_name)
        times = 3 if use_graphs or profile else 1
        if is_prompt:
            seqs = [self.create_dummy_seq_group_metadata(i, seq_len, is_prompt) for i in range(batch_size)]
        else:
            # FIXME: seq_len is actually number of blocks
            blocks = [seq_len // batch_size for _ in range(batch_size)]
            blocks[0] += seq_len % batch_size
            seqs = [self.create_dummy_seq_group_metadata(i, b * self.block_size - 1, is_prompt) for i, b in enumerate(blocks)]
        torch.hpu.synchronize()
        profiler = None
        if profile and self.is_driver_worker:
            profiler = setup_profiler()
            profiler.start()
        self.profiler.start('internal', scenario_name)
        for _ in range(times):
            self.execute_model(seqs, kv_caches, warmup_mode=True)
            torch.hpu.synchronize()
            if profiler:
                profiler.step()
        if profiler:
            profiler.stop()
        self.profiler.end()
        gc.collect()

    def log_warmup(self, phase, i, max_i, batch_size, seq_len):
        free_mem = format_bytes(HabanaMemoryProfiler.current_free_device_memory())
        logger.info(f"[Warmup][{phase}][{i+1}/{max_i}] batch_size:{batch_size} seq_len:{seq_len} free_mem:{free_mem}")

    def warmup_all_buckets(self, buckets, is_prompt, kv_caches):
        for i, (batch_size, seq_len) in enumerate(reversed(buckets)):
            self.log_warmup('Prompt' if is_prompt else 'Decode', i, len(buckets), batch_size, seq_len)
            self.warmup_scenario(batch_size, seq_len, is_prompt, kv_caches)

    def warmup_graphs(self, strategy, buckets, is_prompt, kv_caches, available_mem):
        total_batch_seq = 0.001
        total_mem = 0
        idx = 0
        phase = f'Graph/{"Prompt" if is_prompt else "Decode"}'
        num_candidates = len(buckets)

        if strategy == 'min_tokens':
            ordering = lambda b: (b[0] * b[1], b[1], b[0])
        elif strategy == 'max_bs':
            ordering = lambda b: (-b[0], b[1])
        else:
            raise NotImplementedError(f'Unsupported graph allocation strategy: {strategy}')
        buckets = list(sorted(buckets, key=ordering))

        for idx, (batch_size, seq_len) in enumerate(buckets):
            # Graph memory usage is proportional to seq dimension in a batch
            batch_seq = batch_size * seq_len if is_prompt else batch_size
            mem_estimate = batch_seq / total_batch_seq * total_mem
            if mem_estimate >= available_mem:
                continue
            self.graphed_buckets.add((batch_size, seq_len, is_prompt))
            self.log_warmup(phase, idx, num_candidates, batch_size, seq_len)
            with HabanaMemoryProfiler() as mem_prof:
                self.warmup_scenario(batch_size, seq_len, is_prompt, kv_caches)
            used_mem = align_workers(mem_prof.consumed_device_memory, torch.distributed.ReduceOp.MAX)
            available_mem -= used_mem
            total_mem += used_mem
            total_batch_seq += batch_seq
        graphed = list(c[:2] for c in self.graphed_buckets if c[2] == is_prompt)
        if num_candidates == 0:
            num_candidates = 1
        logger.info(f'{phase} captured:{len(graphed)} ({100 * len(graphed) / num_candidates:.1f}%) used_mem:{format_bytes(total_mem)} buckets:{sorted(list(graphed))}')

    @torch.inference_mode()
    def warmup_model(self, kv_caches: List[torch.Tensor]) -> None:
        if profile := os.environ.get('VLLM_PT_PROFILE', None):
            phase, bs, seq_len, graphs = profile.split('_')
            is_prompt = phase == 'prompt'
            bs = int(bs)
            seq_len = int(seq_len)
            graphs = graphs == 't'
            if graphs:
                self.graphed_buckets.add((bs, seq_len, is_prompt))
            self.warmup_scenario(bs, seq_len, is_prompt, kv_caches, True)
            assert False
        if self.skip_warmup:
            logger.info("Skipping warmup...")
            return
        self.profiler.start('internal', 'warmup')
        max_blocks = kv_caches[0][0].size(0)

        self.prompt_buckets = generate_prompt_buckets(self.prompt_bs_bucket_cfg, self.prompt_seq_bucket_cfg)
        logger.info(f"Generated {len(self.prompt_buckets)} prompt buckets: {list(sorted(self.prompt_buckets))}")

        self.decode_buckets = generate_decode_buckets(self.decode_bs_bucket_cfg, self.decode_block_bucket_cfg, max_blocks)
        logger.info(f"Generated {len(self.decode_buckets)} decode buckets: {list(sorted(self.decode_buckets))}")

        start_mem = HabanaMemoryProfiler.current_device_memory_usage()
        start_time = time.perf_counter()
        self.warmup_all_buckets(self.prompt_buckets, True, kv_caches)
        self.warmup_all_buckets(self.decode_buckets, False, kv_caches)

        if not self.enforce_eager:
            mem_margin = 1.0 - float(os.environ.get('VLLM_GRAPH_MEM_MARGIN', '0.02'))
            free_mem = mem_margin * HabanaMemoryProfiler.current_free_device_memory()
            free_mem = align_workers(free_mem, torch.distributed.ReduceOp.MIN)
            prompt_graph_mem_ratio = float(os.environ.get('VLLM_GRAPH_PROMPT_RATIO', '0.5'))
            prompt_available_memory = prompt_graph_mem_ratio * free_mem
            decode_available_memory = free_mem - prompt_available_memory
            prompt_strategy = 'min_tokens'
            decode_strategy = os.environ.get('VLLM_GRAPH_DECODE_STRATEGY', 'max_bs')
            self.warmup_graphs(prompt_strategy, self.prompt_buckets, True, kv_caches, prompt_available_memory)
            self.warmup_graphs(decode_strategy, self.decode_buckets, False, kv_caches, decode_available_memory)

        end_time = time.perf_counter()
        end_mem = HabanaMemoryProfiler.current_device_memory_usage()
        elapsed_time = end_time - start_time
        logger.info(f"Warmup finished in {elapsed_time:.0f} secs, allocated {format_bytes(end_mem - start_mem)} of device memory")
        self.profiler.end()

    def shutdown_hqt(self):
        print('hqt shutdown')
        if model_config := getattr(self, "model_config", None):
            if getattr(model_config, "quantization", None) == 'hqt':
                print('hqt shutdown start')
                import habana_quantization_toolkit
                if habana_quantization_toolkit is not None:
                    habana_quantization_toolkit.finish_measurements(self.model.model)
                print('hqt shutdown')

    def __del__(self):
        self.shutdown_hqt()

    @property
    def vocab_size(self) -> int:
        return self.model_config.get_vocab_size()

def _maybe_wrap_in_hpu_graph(model):
    return htorch.hpu.wrap_in_hpu_graph(
        model, disable_tensor_cache=True
    ) if htorch.utils.internal.is_lazy() else model


class HabanaProfilerCounterHelper():
    def __init__(self):
        self.niter = 0
        self.average_real_throughput = None
        self.logged_once = False
    
    def get_counter_dict(self, cache_config, duration, seq_len, batch_size_padded, real_batch_size, seq_group_metadata_list, is_prompt):
        throughput = batch_size_padded / (duration / 1e6)
        throughput_effective = real_batch_size / (duration / 1e6)
        real_seq_lens = [len(seq_data.prompt_token_ids) + len(seq_data.output_token_ids) for seq_group_metadata in seq_group_metadata_list for seq_data in seq_group_metadata.seq_data.values()]
        real_max_seq_len = max(real_seq_lens)
        real_num_tokens = sum(real_seq_lens)
        padded_num_tokens = batch_size_padded * seq_len
        batch_token_utilization = real_num_tokens / padded_num_tokens
        if self.average_real_throughput is None:
            self.average_real_throughput = throughput_effective
        else: # https://www.heikohoffmann.de/htmlthesis/node134.html
            self.average_real_throughput = self.average_real_throughput + 1/(self.niter+1) * (throughput_effective-self.average_real_throughput)
        phase = "prompt" if is_prompt else "decode"
        counters = {
            f'{phase}_bucket_batch_size': batch_size_padded,
            f'{phase}_batch_size': real_batch_size,
            f'{phase}_bucket_seq_len': seq_len,
            f'{phase}_seq_len': real_max_seq_len,
            f'{phase}_bucket_gen_throughput': throughput,
            f'{phase}_real_gen_throughput': throughput_effective,
            f'{phase}_batch_token_utilization': batch_token_utilization,
            'average_real_throughput': self.average_real_throughput,
            'engine_iteration': self.niter,
        }
        self.niter += 1 
        if is_prompt:
            prompt_seq_lens = [len(seq_data.prompt_token_ids) for seq_group_metadata in seq_group_metadata_list for seq_data in seq_group_metadata.seq_data.values()]
            prompt_bucket_in_throughput = (seq_len*batch_size_padded) / (duration / 1e6) 
            prompt_real_in_throughput = sum(prompt_seq_lens) / (duration / 1e6) 
            counters[f'{phase}_bucket_in_throughput'] = prompt_bucket_in_throughput
            counters[f'{phase}_real_in_throughput'] = prompt_real_in_throughput

        # KV cache might not be created yet (e.g. for profiling run)
        if cache_config.num_gpu_blocks is not None and cache_config.num_gpu_blocks != 0:
            cache_num_blocks_used = [math.ceil(sl/cache_config.block_size) for sl in real_seq_lens]
            cache_total_num_blocks_used = sum(cache_num_blocks_used)
            num_cache_blocks = cache_config.num_gpu_blocks 
            cache_total_num_free_blocks = num_cache_blocks - cache_total_num_blocks_used
            cache_computed_utilization = cache_total_num_blocks_used / num_cache_blocks
            max_blocks_per_seq = math.ceil(seq_len/cache_config.block_size)
            batch_block_utilization = cache_total_num_blocks_used / (batch_size_padded * max_blocks_per_seq)
            counters['cache_num_blocks_used'] = cache_total_num_blocks_used
            counters['cache_num_free_blocks'] = cache_total_num_free_blocks
            counters['cache_computed_utilization'] = cache_computed_utilization
            counters[f'{phase}_batch_block_utilization'] = batch_block_utilization
        if not self.logged_once:
            counters['const_cache_num_blocks'] = cache_config.num_gpu_blocks
            counters['const_gpu_memory_utilization'] = cache_config.gpu_memory_utilization
            counters['const_block_size'] = cache_config.block_size
            self.logged_once = True
        return counters
