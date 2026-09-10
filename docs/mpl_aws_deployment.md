# MPL AI deployment on AWS (beginner runbook)

This design keeps claim data private. The existing Django EC2 instance stores and retrieves claim evidence. A separate private GPU EC2 instance runs `Ministral-3-3B-Instruct-2512`. The browser never calls the model directly.

## 1. Before starting

You need:

- access to the AWS account and the VPC containing the Django server;
- SSH access to both EC2 instances;
- access to the private Hugging Face model files if the repository requires acceptance/authentication;
- a database backup and a maintenance window;
- at least 50 human-approved MPL analyses before any fine-tuning (200+ preferred).

Do not put claim/email data, API keys, `.env` files, model caches, or training JSONL in Git.

## 2. Create security groups

In AWS Console, open **EC2 → Security Groups**.

1. Note the security-group ID attached to the existing Django server, called `BACKEND_SG` below.
2. Create `mir-mpl-ai-sg` in the same VPC.
3. Add one inbound rule to `mir-mpl-ai-sg`:
   - Type: Custom TCP
   - Port: `8080`
   - Source: `BACKEND_SG` (select the security group, not an IP address)
4. Do not expose port 8080 to `0.0.0.0/0`.
5. Add SSH port 22 only from your office/VPN IP, or use AWS Systems Manager Session Manager.
6. Keep normal outbound HTTPS enabled while installing/downloading. Restrict it later if your operations policy requires it.

## 3. Launch the GPU instance

Open **EC2 → Launch instance**:

1. Name: `mir-mpl-ai-prod`.
2. AMI: AWS Deep Learning Base GPU AMI (Ubuntu).
3. Instance type: `g4dn.xlarge` (NVIDIA T4 16 GB VRAM, 4 vCPU, 16 GB RAM).
4. Network: the same VPC as Django.
5. Subnet: a private subnet. Do not assign a public IPv4 address when Session Manager or a bastion is available.
6. Security group: `mir-mpl-ai-sg`.
7. Storage: 100 GB encrypted gp3. Select the account KMS key if required.
8. Require IMDSv2 in advanced metadata options.
9. Add tags such as `Application=MIR`, `Component=MPL-AI`, `Environment=Production`.
10. Launch, then record its private IPv4 address as `AI_PRIVATE_IP`.

If the instance cannot download packages from a private subnet, provide controlled outbound access through a NAT gateway temporarily. Never solve this by exposing model port 8080 publicly.

## 4. Verify the GPU

Connect to the GPU instance and run:

```bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
nvidia-smi
```

The command must show an NVIDIA T4 and a healthy driver. Stop here if it does not.

## 5. Build llama.cpp with CUDA

On the GPU instance:

```bash
sudo apt-get update
sudo apt-get install -y git cmake build-essential curl libcurl4-openssl-dev
sudo mkdir -p /opt/mir-ai
sudo chown ubuntu:ubuntu /opt/mir-ai
cd /opt/mir-ai
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j 4
./build/bin/llama-server --version
```

For reproducible production deployments, record the tested llama.cpp commit and later check it out explicitly rather than continuously using the latest commit.

## 6. Configure the private model service

Generate a service secret on the backend host or your secure workstation:

```bash
openssl rand -hex 32
```

Copy `deploy/mpl-ai.service` from the backend repository to the GPU instance, then run there:

```bash
sudo cp /path/to/mpl-ai.service /etc/systemd/system/mpl-ai.service
sudo install -m 600 -o root -g root /dev/null /etc/mpl-ai-server.env
sudo nano /etc/mpl-ai-server.env
```

Enter exactly one line, using the generated value:

```text
MPL_AI_API_KEY=REPLACE_WITH_THE_LONG_RANDOM_SECRET
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mpl-ai.service
sudo journalctl -u mpl-ai.service -f
```

The first start downloads `mistralai/Ministral-3-3B-Instruct-2512-GGUF` in `Q4_K_M`; it can take several minutes. If Hugging Face requires authentication, authenticate only on this host using a read-only token. Do not put that token in the systemd unit or Git.

In another terminal on the GPU host:

```bash
set -a
source /etc/mpl-ai-server.env
set +a
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8080/v1/models \
  -H "Authorization: Bearer $MPL_AI_API_KEY"
```

## 7. Connect Django to the GPU service

On the existing Django EC2 instance:

```bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd /var/www/835_to_MIR_backend
git pull origin main
```

Create the protected environment file:

```bash
sudo install -m 640 -o root -g www-data /dev/null /etc/mir-mpl-ai.env
sudo nano /etc/mir-mpl-ai.env
```

Enter:

```text
MPL_AI_BASE_URL=http://AI_PRIVATE_IP:8080/v1
MPL_AI_API_KEY=THE_SAME_LONG_RANDOM_SECRET
MPL_AI_MODEL=ministral-3-3b-instruct-2512
MPL_AI_TIMEOUT_SECONDS=120
```

Replace `AI_PRIVATE_IP`. Test connectivity without printing the secret:

```bash
set -a
source /etc/mir-mpl-ai.env
set +a
curl -sS "$MPL_AI_BASE_URL/models" \
  -H "Authorization: Bearer $MPL_AI_API_KEY"
```

## 8. Apply the database migration

The service environment that already supplies `SECRET_KEY`, PostgreSQL credentials, and the other production settings must be loaded before Django commands. Do not invent replacement values on the command line.

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
set -a
source /etc/mir-mpl-ai.env
set +a
python -m py_compile edi835/mpl_notices.py edi835/mpl_views.py
python manage.py migrate --plan
python manage.py migrate
python manage.py check --deploy
```

If `SECRET_KEY` is reported missing, load the same EnvironmentFile used by `mir.service`, or run the command through the deployment mechanism that already supplies those values. Never paste the production key into shell history.

## 9. Install and start the queue worker

The worker file expects the normal application environment plus `/etc/mir-mpl-ai.env`. If the existing Django service uses another EnvironmentFile, add that same file to the worker unit as a second `EnvironmentFile=` entry.

```bash
sudo cp deploy/mpl-notice-worker.service /etc/systemd/system/mpl-notice-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now mpl-notice-worker.service
sleep 3
sudo systemctl status mpl-notice-worker.service --no-pager
sudo journalctl -u mpl-notice-worker.service --since "5 minutes ago" --no-pager
```

Restart Django after migration:

```bash
sudo systemctl restart mir.service
sleep 5
sudo systemctl status mir.service --no-pager
sudo journalctl -u mir.service --since "5 minutes ago" --no-pager
```

## 10. Deploy the frontend

The MPL UI is on frontend branch `master`. If Vercel automatically deploys `master`, watch the production deployment. Otherwise trigger the production deployment using the repository's normal Vercel workflow. Confirm the frontend origin is still allowed by Django/nginx CORS. No new browser-to-GPU CORS rule is needed because only Django calls the GPU.

## 11. End-to-end acceptance test

1. Log in as a client user with known 837/835/MIR/reconciliation data.
2. Open **MPL Notices** and click **Add Email**.
3. Paste an actual supported subject, for example `MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC`.
4. Paste the full Outlook body. Enter exact claim numbers if the email body does not contain them.
5. Submit.
6. Confirm status progresses from `RECEIVED` through processing to `COMPLETED`, or safely to `REVIEW REQUIRED`/claim selection.
7. Confirm the claim timeline contains only files belonging to that client.
8. Download each related 837, 835, MIR, and reconciliation file and verify it is the expected archive record.
9. Confirm every issue cites evidence and every proposed correction is from the approved action catalogue.
10. Approve only an accurate analysis. Mark inaccurate results **Changes Required**.
11. Repeat as a different client and confirm there is no cross-client visibility.
12. Stop `mpl-ai.service` briefly and verify the deterministic fallback still produces a safe analysis rather than blocking the workflow; then start it again.

Useful monitoring commands:

```bash
sudo journalctl -u mir.service -f
sudo journalctl -u mpl-notice-worker.service -f
sudo journalctl -u mpl-ai.service -f
nvidia-smi -l 2
```

## 12. Fine-tuning lifecycle

Fine-tuning must not start with the five sample emails alone. They define format, not enough correct claim-resolution behavior. First collect reviewed production examples.

On the backend host, after at least 50 analyses have been explicitly approved:

```bash
sudo mkdir -p /var/lib/mir-ai/training
sudo chown ubuntu:www-data /var/lib/mir-ai/training
cd /var/www/835_to_MIR_backend
source venv/bin/activate
python manage.py export_mpl_finetune_dataset \
  /var/lib/mir-ai/training/mpl-approved.jsonl \
  --minimum 50
```

The exporter includes only approved analyses and de-identifies common identifiers. A qualified reviewer must still inspect the entire export for PHI. Keep it encrypted and outside Git. Split related email threads into the same partition so duplicates cannot leak from training to test: 80% train, 10% validation, 10% final test.

Use QLoRA on a separate controlled training run. Start with:

- FP16 compute on T4;
- batch size 1, gradient accumulation 16;
- learning rate `1e-4`;
- LoRA rank 16, alpha 32, dropout 0.05;
- gradient checkpointing;
- 2 epochs first, at most 3 before evaluating;
- only the language component and only the supported adapter target modules found in the pinned model version.

Pin the exact Transformers, PEFT, TRL, bitsandbytes, and model revisions tested together. Do not guess adapter module names: print the loaded language model's named modules and verify the chosen attention/MLP projections exist. Preserve the base model license and access terms.

Reject the adapter unless the held-out final test has all of the following:

- 100% parseable response JSON;
- zero invented claim IDs, filenames, issue codes, or action choices;
- zero cross-client evidence;
- zero promises that a claim will be approved;
- primary-issue accuracy no worse than the base model;
- successful review by the claims/EDI owner.

After it passes, merge the adapter into a copy of the base language model, convert it to GGUF, quantize it to Q4_K_M, and store it in an encrypted versioned model directory. Change the llama.cpp service from `-hf ...` to `--model /opt/mir-ai/models/mpl-ministral-3b-YYYY-MM-v1.Q4_K_M`. Run it in shadow mode against the base model, then promote it. Keep the previous model and service definition for immediate rollback.

## 13. Backup, rollback, and cost control

- Take an encrypted database snapshot before migration.
- Keep the previous frontend deployment and backend commit available.
- To disable AI without disabling MPL intake, stop `mpl-ai.service`; deterministic evidence analysis remains available.
- To stop GPU cost outside testing, stop the GPU EC2 instance. EBS storage continues to incur cost.
- Create AWS Budgets alerts and CloudWatch alarms for instance status, disk, worker failures, and repeated model timeouts.
- Never auto-apply the model's recommended correction to a claim. Human approval is mandatory.
