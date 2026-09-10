# MPL Qwen3 0.6B tuning policy

Use prompt engineering first. Fine-tuning is allowed only after at least 50 human-approved examples; 200 or more are preferred. Raw or unreviewed model output must never become a training label.

Export approved, de-identified JSONL with `export_mpl_finetune_dataset`. Manually review all output for PHI and keep related email threads together in the 80/10/10 train, validation, and test partitions.

Train a LoRA/QLoRA adapter for `Qwen/Qwen3-0.6B` on a temporary GPU, never the production t3.medium. Start conservatively with rank 16, alpha 32, dropout 0.05, two epochs, gradient checkpointing, and a low learning rate. Pin every model and library revision.

Reject the candidate unless the untouched test set gives 100% valid JSON, no invented claims/files/codes/actions, no cross-client evidence, no approval guarantees, and issue accuracy at least equal to the untuned model. After human acceptance, merge, convert to GGUF Q4_K_M, version the artifact, run shadow comparisons, and preserve the previous model for rollback.
