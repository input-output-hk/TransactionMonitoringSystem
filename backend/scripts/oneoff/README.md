# One-off rescore scripts

Operational artifacts. Each script in this directory was written to fix a
specific historical batch of misclassified `tx_class_scores` rows after a
scorer-logic change landed. They are not idempotent and they are not part
of the normal analysis pipeline.

Do not run unattended. Read the docstring at the top of the file you intend
to run, confirm the targeted partition / class / date range still matches
the current DB state, and prefer `--apply` only after a dry-run.

After the corresponding cleanup has run successfully on every environment
(preprod, staging, production), the script may be deleted in a follow-up.
