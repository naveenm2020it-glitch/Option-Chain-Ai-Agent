from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_nse_collection_workflow_uses_real_secret_expressions_and_b2_first():
    workflow = (WORKFLOWS / "nse-download-check.yml").read_text(encoding="utf-8")

    assert chr(92) + "$" + "{{" not in workflow
    assert "$" + "{{ secrets.B2_ENDPOINT_URL }}" in workflow
    assert "$" + "{{ secrets.B2_APPLICATION_KEY_ID }}" in workflow
    assert "$" + "{{ secrets.B2_APPLICATION_KEY }}" in workflow
    assert "$" + "{{ secrets.B2_BUCKET_NAME }}" in workflow
    assert "Upload durable archive to Backblaze B2" in workflow
    assert "vars.ENABLE_DAILY_B2_ARCHIVE == 'true'" in workflow
    assert "scripts/nse_download_check.py" in workflow
    assert "range_shadow" not in workflow
    assert "range-bundle" not in workflow


def test_daily_and_historical_workflows_share_b2_writer_lock():
    daily = (WORKFLOWS / "nse-download-check.yml").read_text(encoding="utf-8")
    historical = (WORKFLOWS / "nifty-historical-backfill.yml").read_text(encoding="utf-8")

    assert "group: nse-b2-archive" in daily
    assert "group: nse-b2-archive" in historical
    assert "cancel-in-progress: false" in daily
    assert "cancel-in-progress: false" in historical


def test_daily_collection_does_not_run_or_load_any_model():
    workflow = (WORKFLOWS / "nse-download-check.yml").read_text(encoding="utf-8")
    assert "scripts/nse_download_check.py" in workflow
    assert "scripts/train_" not in workflow
    assert "range_shadow" not in workflow
    assert "Install Range inference dependencies" not in workflow


def test_historical_integrity_audit_is_manual_read_only_and_bounded():
    workflow = (WORKFLOWS / "nifty-historical-integrity-audit.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "\n  push:" not in workflow
    assert "--max-archives" in workflow
    assert "nse_historical_backfill.py" not in workflow
    assert "train_adaptive_intelligence.py" not in workflow


def test_phase1_all_strike_acceptance_audit_is_separate_and_bounded():
    workflow = (WORKFLOWS / "phase1-3-acceptance-audit.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "phase1-3/acceptance-audit-notes" in workflow
    assert "scripts/run_full_historical_audit.py" in workflow
    assert "--batch-size 25" in workflow
    assert "--max-batches 25" in workflow
    assert "secrets.B2_APPLICATION_KEY" in workflow
    assert "nifty-all-strike-historical-audit-" in workflow


def test_phase3_remote_ingestion_is_manual_and_uses_protected_secret():
    workflow = (WORKFLOWS / "phase3-remote-historical-ingestion.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "secrets.PAPER_INGEST_TOKEN" in workflow
    assert "nifty-all-strike-historical-audit-" in workflow
    assert "scripts/ingest_phase3_remote.py" in workflow
    assert "option-chain-ai-agent.onrender.com" in workflow
    assert "--batch-size 10" in workflow
    assert "echo" not in workflow.lower() or "PAPER_INGEST_TOKEN" not in workflow.lower()
