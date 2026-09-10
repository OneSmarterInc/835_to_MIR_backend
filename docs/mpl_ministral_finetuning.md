# MPL Ministral 3 3B fine-tuning runbook

Fine-tuning is permitted only after the evidence-first MPL workflow has collected at least 50 human-approved examples. Prefer 200 or more. Raw, unreviewed production output must not be used as a training label.

## Dataset preparation

On the backend host, approve accurate analyses in the MPL UI. Export a de-identified dataset outside the repository:

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
python manage.py export_mpl_finetune_dataset /var/lib/mir-ai/training/mpl-approved.jsonl --minimum 50
```

Manually inspect the JSONL for PHI before transferring it to the isolated training host. Split by email/claim family, not randomly by duplicate email thread: 80% train, 10% validation, 10% test.

## Training method

Use QLoRA/LoRA rather than changing all base-model weights. The base checkpoint is `mistralai/Ministral-3-3B-Instruct-2512`. Train only the language component on the chat JSONL. Use FP16 compute on an NVIDIA T4, gradient checkpointing, batch size 1, gradient accumulation 16, learning rate `1e-4`, LoRA rank 16, LoRA alpha 32, dropout 0.05, and no more than 2–3 epochs initially.

Because Ministral 3 support in Transformers is evolving, install and pin the exact tested Transformers/PEFT/TRL commits on the training host. Confirm that the adapter targets resolve against `Mistral3ForConditionalGeneration` before starting. Do not silently fall back to a different architecture.

## Acceptance gate

The candidate adapter must be rejected unless the held-out set has:

- 100% valid JSON;
- zero invented claim IDs or filenames;
- zero unknown issue codes;
- zero actions outside the approved catalogue;
- zero cross-client evidence;
- no claim-approval guarantees;
- at least the base model's accuracy for primary issue classification.

After passing, merge the LoRA adapter into a copy of the base language model, export/quantize the approved model for the inference runtime, name it with a version such as `mpl-ministral-3b-2026-09-v1`, and keep the previous model available for rollback. Set `MPL_AI_MODEL` to the served model name only after a shadow comparison in production.
