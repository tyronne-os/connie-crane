# GCP GPU Recovery — berylize-node (2026-09-05)

## What Happened

Attempted to download MiniMax-H3 (~32GB) directly to the boot disk of `berylize-node`.
The boot disk (100GB, 87% full) ran out of space mid-download, crashing `sshd` and
causing GCP to preempt the instance twice.

## Root Cause

- `berylize-node` is a **preemptible** g2-standard-4 instance — GCP can reclaim it at any time
- Boot disk had ~13GB free; text encoder alone is 14.6GB
- `hf_hub_download` default behavior creates a `.cache/` copy alongside the final file,
  doubling the disk footprint mid-download
- `us-east1-c` zone had no `pd-ssd` or `pd-balanced` resize capacity at time of incident

## Resolution

1. **Restarted the instance** via `gcloud compute instances start berylize-node --zone=us-east1-c`
2. **Created a 50GB secondary disk** (pd-standard, only type available in zone at the time):
   ```bash
   gcloud compute disks create h3-storage --zone=us-east1-c --size=50GB --type=pd-standard
   gcloud compute instances attach-disk berylize-node --zone=us-east1-c --disk=h3-storage
   ```
3. **Formatted and mounted** inside the VM:
   ```bash
   sudo mkfs.ext4 -F /dev/nvme0n2
   sudo mkdir -p /mnt/h3storage
   sudo mount /dev/nvme0n2 /mnt/h3storage
   sudo chown $(whoami):$(whoami) /mnt/h3storage
   ```
4. **Downloaded all H3 files to secondary disk** using `local_dir_use_symlinks=False`
   to avoid the double-copy cache issue:
   ```bash
   nohup python3 -c "
   from huggingface_hub import hf_hub_download
   dest='/mnt/h3storage/minimax-h3'
   jobs=[
       ('leejet/MiniMax-H3-GGUF','minimax_h3_fl2va_pruned-Q4_K_M.gguf'),
       ('Abiray/MiniMax-H3-GGUF','text_encoders/qwen3vl_32b_minimax_h3-Q4_K_M.gguf'),
       ('Abiray/MiniMax-H3-GGUF','vae/minimax_h3_video_vae_fp16.safetensors'),
       ('Abiray/MiniMax-H3-GGUF','vae/minimax_h3_audio_vae_fp32.safetensors'),
   ]
   for repo,f in jobs:
       hf_hub_download(repo_id=repo,filename=f,local_dir=dest,local_dir_use_symlinks=False)
   " > /tmp/h3.log 2>&1 &
   ```
5. **Cleared partial local vault copy** on laptop (`/mnt/NOBILITY_VAULT/models/minimax-h3`)
6. **Updated CRANE VIDEO_ROSTER** paths from vault to `/mnt/h3storage/minimax-h3`

## Final State

| File | Size | Location |
|------|------|----------|
| `minimax_h3_fl2va_pruned-Q4_K_M.gguf` | 11GB | `/mnt/h3storage/minimax-h3/` |
| `text_encoders/qwen3vl_32b_minimax_h3-Q4_K_M.gguf` | 14.6GB | `/mnt/h3storage/minimax-h3/text_encoders/` |
| `vae/minimax_h3_video_vae_fp16.safetensors` | ~3GB | `/mnt/h3storage/minimax-h3/vae/` |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | ~1.5GB | `/mnt/h3storage/minimax-h3/vae/` |
| **Total** | **~30GB** | **`/mnt/h3storage` (49GB disk, 47GB free after)** |

## Rules Going Forward

- **Always download large models to `/mnt/h3storage`**, never to home or boot disk
- **Always use `local_dir_use_symlinks=False`** with `hf_hub_download` to avoid cache doubling
- **Always `nohup ... &`** so downloads survive SSH disconnects
- Mount the secondary disk on reboot: add to `/etc/fstab` with UUID
  ```bash
  # get UUID
  sudo blkid /dev/nvme0n2
  # add to /etc/fstab:
  UUID=<uuid>  /mnt/h3storage  ext4  defaults,nofail  0  2
  ```
- `berylize-node` is **preemptible** — expect random terminations. Keep CRANE's GPU meter
  idle timeout at 10 minutes so you're not billed for dead instances.

## Instance Details

- **Instance**: `berylize-node` (g2-standard-4, NVIDIA L4 24GB VRAM, preemptible)
- **Zone**: `us-east1-c`
- **Project**: `posh-eden`
- **Boot disk**: 100GB nvme0n1 (87% used — do not store models here)
- **Model disk**: 50GB `h3-storage` nvme0n2, mounted at `/mnt/h3storage`
