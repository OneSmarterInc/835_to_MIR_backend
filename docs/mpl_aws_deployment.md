# MPL AI on the existing AWS t3.medium

This setup runs Django, the MPL worker, and CPU-only Qwen3 0.6B on the same EC2 server. The model listens only on `127.0.0.1`; it is never exposed publicly. Django performs claim lookup and validation. The model only explains verified evidence.

## Capacity limits

- Model: `Qwen/Qwen3-0.6B-GGUF`, `Q4_K_M`
- Runtime: llama.cpp, CPU only
- Context: 2,048 tokens
- Threads and parallel requests: 1
- Model memory ceiling: 1.7 GB
- Do not fine-tune on this instance

Take database and EBS snapshots first. Monitor CPU credit balance because t3 instances are burstable.

## 1. Verify the server

```bash
ssh ubuntu@YOUR_SERVER
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd /var/www/835_to_MIR_backend
free -h
nproc
df -h /
```

Stop if free/available memory is consistently below 1.8 GB or disk space is below 10 GB.

## 2. Add 4 GB emergency swap

```bash
swapon --show
```

If it shows no swap:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-mir-ai.conf
sudo sysctl --system
free -h
```

The root EBS volume must be encrypted because swap can contain application data.

## 3. Pull and migrate the backend

Use the production environment already configured for `mir.service`. Never type production secrets into shell history.

```bash
cd /var/www/835_to_MIR_backend
git pull origin main
source venv/bin/activate
python -m py_compile edi835/mpl_notices.py edi835/mpl_views.py
python manage.py migrate --plan
python manage.py migrate
python manage.py check --deploy
```

If `SECRET_KEY` is missing, load the real environment used by `mir.service`; do not invent a temporary key.

## 4. Build llama.cpp for CPU

```bash
sudo apt-get update
sudo apt-get install -y git cmake build-essential curl libcurl4-openssl-dev
sudo mkdir -p /opt/mir-ai
sudo chown ubuntu:ubuntu /opt/mir-ai
cd /opt/mir-ai
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j 2
./build/bin/llama-server --version
git rev-parse HEAD
```

Record and later pin the tested llama.cpp commit.

## 5. Configure protected secrets

Generate one secret:

```bash
openssl rand -hex 32
```

Create `/etc/mpl-ai-server.env`:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/mpl-ai-server.env
sudo nano /etc/mpl-ai-server.env
```

```text
MPL_AI_API_KEY=YOUR_RANDOM_SECRET
```

Create `/etc/mir-mpl-ai.env` using the same secret:

```bash
sudo install -m 640 -o root -g www-data /dev/null /etc/mir-mpl-ai.env
sudo nano /etc/mir-mpl-ai.env
```

```text
MPL_AI_BASE_URL=http://127.0.0.1:8080/v1
MPL_AI_API_KEY=YOUR_RANDOM_SECRET
MPL_AI_MODEL=qwen3-0.6b-instruct-q4_k_m
MPL_AI_TIMEOUT_SECONDS=180
```

## 6. Start the model

```bash
cd /var/www/835_to_MIR_backend
sudo cp deploy/mpl-ai.service /etc/systemd/system/mpl-ai.service
sudo systemctl daemon-reload
sudo systemctl enable --now mpl-ai.service
sudo journalctl -u mpl-ai.service -f
```

The first start downloads Q4_K_M. Wait for the listening message, then press `Ctrl+C` to leave the log.

```bash
set -a
source /etc/mpl-ai-server.env
set +a
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8080/v1/models \
  -H "Authorization: Bearer $MPL_AI_API_KEY"
free -h
```

## 7. Configure and start the worker

Inspect Django's real environment:

```bash
sudo systemctl cat mir.service
```

The worker needs the same `SECRET_KEY`, database, and production variables. If the Django unit already uses an `EnvironmentFile`, reference it in `mpl-notice-worker.service`. Otherwise create `/etc/mir-backend.env` containing equivalent `NAME=value` lines:

```bash
sudo install -m 640 -o root -g www-data /dev/null /etc/mir-backend.env
sudo nano /etc/mir-backend.env
```

Do not copy `[Service]` headings into an environment file and do not commit it.

```bash
cd /var/www/835_to_MIR_backend
sudo cp deploy/mpl-notice-worker.service /etc/systemd/system/mpl-notice-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now mpl-notice-worker.service
sudo systemctl restart mir.service
sleep 5
sudo systemctl status mpl-ai.service --no-pager
sudo systemctl status mpl-notice-worker.service --no-pager
sudo systemctl status mir.service --no-pager
```

```bash
sudo journalctl -u mpl-ai.service --since "10 minutes ago" --no-pager
sudo journalctl -u mpl-notice-worker.service --since "10 minutes ago" --no-pager
sudo journalctl -u mir.service --since "10 minutes ago" --no-pager
```

## 8. Deploy and test the frontend

Deploy frontend `master` through the existing Vercel project. Port 8080 needs no CORS or nginx exposure because only Django calls localhost.

1. Log in as a client and open **MPL Notices**.
2. Enter an actual supported subject, full email body, and exact claim number.
3. Confirm processing ends in `COMPLETED`, `REVIEW REQUIRED`, or claim selection—not permanently `RECEIVED`.
4. Verify the 837, MIR, 835, and reconciliation timeline and downloads.
5. Confirm every recommendation cites displayed findings.
6. Approve an accurate result or mark it **Changes Required**.
7. Repeat with another client to verify tenant isolation.

## 9. Monitor and protect Django

```bash
watch -n 2 'free -h; echo; uptime; echo; systemctl is-active mir.service mpl-ai.service mpl-notice-worker.service'
```

If Django slows down, memory remains above 90%, swap grows continuously, or CPU credits approach zero, stop only AI:

```bash
sudo systemctl stop mpl-ai.service
```

Deterministic MPL analysis remains available. Restart later with `sudo systemctl start mpl-ai.service`.

## 10. Fine-tuning later

Start with the constrained prompt. Do not tune from five format samples. After at least 50 approved analyses (200+ preferred), export outside Git:

```bash
sudo mkdir -p /var/lib/mir-ai/training
sudo chown ubuntu:www-data /var/lib/mir-ai/training
cd /var/www/835_to_MIR_backend
source venv/bin/activate
python manage.py export_mpl_finetune_dataset \
  /var/lib/mir-ai/training/mpl-approved.jsonl \
  --minimum 50
```

Manually inspect all output for PHI. Train a LoRA/QLoRA adapter only on a temporary GPU host. Reject it unless held-out evaluation has valid JSON, no invented identifiers/files/codes/actions, no tenant leakage, no approval promises, and accuracy at least as good as the prompt-only model. Merge an accepted adapter, convert it to GGUF Q4_K_M, version it, compare it in shadow mode, and retain the current model for rollback.
