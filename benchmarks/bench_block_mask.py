import itertools
from dataclasses import dataclass
from typing import List, Callable

import torch
from tabulate import tabulate
from tqdm import tqdm

try:
    import transformer_nuggets
except ImportError:
    print(
        "Need to install transformer_nuggets for this benchmark. "
        "Run `pip install git+https://github.com/drisspg/transformer_nuggets`"
    )
    # Exit if the dependency is missing
    sys.exit(1)
from transformer_nuggets.utils import benchmark_cuda_function_in_microseconds, profiler, cuda_memory_usage


from attn_gym.masks import  causal_mask, generate_sliding_window, generate_prefix_lm_mask, generate_doc_mask_mod, generate_dilated_sliding_window

from torch.nn.attention.flex_attention import create_block_mask, _mask_mod_signature
import sys

device = torch.device("cuda")

# Needed since changing args to function causes recompiles
torch._dynamo.config.cache_size_limit = 1000


MASK_MOD_MAP = {
    "causal": causal_mask,
    "sliding_window": generate_sliding_window,
    "prefix_lm": generate_prefix_lm_mask,
    "doc_mask_mod": generate_doc_mask_mod,
    "dilated_sliding_window": generate_dilated_sliding_window,
}

@dataclass(frozen=True)
class ExperimentConfig:
    B: int
    H: int
    M: int
    N: int
    mask_mod_name: str


@dataclass(frozen=True)
class ExperimentResult:
    creation_time_ms: float
    memory_bytes: int


@dataclass(frozen=True)
class Experiment:
    config: ExperimentConfig
    result: ExperimentResult


def get_mask_mod(name: str) -> _mask_mod_signature:
    match name:
        case "sliding_window":
            return generate_sliding_window()
        case "prefix_lm":
            return generate_prefix_lm_mask()
        case "doc_mask_mod":
            return generate_doc_mask_mod()
        case "dilated_sliding_window":
            return generate_dilated_sliding_window()
        case _:
            mod = MASK_MOD_MAP[name]
            return mod


def get_configs() -> List[ExperimentConfig]:
    # Define ranges for benchmark parameters
    Bs = [1, 4, 8]
    Hs = [8, 16]
    # Sequence lengths - adjust as needed
    # Using powers of 2 up to a reasonable limit for mask creation
    SeqLens = [1024, 2048, 4096, 8192]
    # Map string names to mask functions
    mask_mods_to_run = list(MASK_MOD_MAP.keys())

    configs = []
    for B, H, S, mask_mod in itertools.product(Bs, Hs, SeqLens, mask_mods_to_run):
        configs.append(
            ExperimentConfig(
                B=B,
                H=H,
                M=S,  # Assuming M=N for simplicity
                N=S,
                mask_mod_name=mask_mod
            )
        )
    return configs

def run_experiment(config: ExperimentConfig) -> ExperimentResult:
    # Find the mask_mod function by name
    assert config.mask_mod_name in MASK_MOD_MAP, f"Mask mod '{config.mask_mod_name}' not found."
    mask_mod_fn = get_mask_mod(config.mask_mod_name)


    # --- Time Benchmarking --- 
    cbm = torch.compile(create_block_mask)
    # Warmup
    for _ in range(10):
        cbm(
            mask_mod_fn, config.B, config.H, config.M, config.N, device=device
        )
    torch.cuda.synchronize(device) 

    creation_time_ms = benchmark_cuda_function_in_microseconds(
        lambda: cbm(
            mask_mod_fn, config.B, config.H, config.M, config.N, device=device
        ),
    )

    torch.cuda.synchronize(device)

    with cuda_memory_usage() as memory_bytes:
        cbm(
            mask_mod_fn, config.B, config.H, config.M, config.N, device=device
        )
   

    return ExperimentResult(
        creation_time_ms=creation_time_ms * 1000,
        memory_bytes=memory_bytes #
    )


def print_results(experiments: List[Experiment]):
    headers = [
        "B",
        "H",
        "M",
        "N",
        "Mask Mod",
        "Creation Time (ms)",
        "Memory (GiB)", 
    ]
    rows = []
    for experiment in experiments:
        rows.append(
            [
                experiment.config.B,
                experiment.config.H,
                experiment.config.M,
                experiment.config.N,
                experiment.config.mask_mod_name,
                f"{experiment.result.creation_time_ms:.4f}",
                f"{experiment.result.memory_bytes:.2f}"
            ]
        )
    # Sort rows for better readability (e.g., by B, H, M, N)
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    print(tabulate(rows, headers=headers, tablefmt="grid"))


def main():
    torch.random.manual_seed(123)
    configs = get_configs()
    results = []
    print(f"Running {len(configs)} benchmark configurations...")
    for config in tqdm(configs):
        try:
            result = run_experiment(config)
            results.append(Experiment(config=config, result=result))
        except Exception as e:
            print(f"Failed to run config {config}: {e}")
            # Optionally skip failed configs or handle differently

    # Use Tabulate to print results
    print_results(results)


if __name__ == "__main__":
    main()

