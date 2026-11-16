from pathlib import Path


def test_nse_collection_workflow_uses_real_secret_expressions_and_b2_first():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    assert chr(92) + "$" + "{{" not in workflow
    assert "$" + "{{ secrets.B2_ENDPOINT_URL }}" in workflow
    assert "$" + "{{ secrets.B2_APPLICATION_KEY_ID }}" in workflow
    assert "$" + "{{ secrets.B2_APPLICATION_KEY }}" in workflow
    assert "$" + "{{ secrets.B2_BUCKET_NAME }}" in workflow
    assert "\n  push:" not in workflow
    assert workflow.index("Upload durable archive to Backblaze B2") < workflow.index(
        "Persist daily report manifest in application database"
    )
    assert "continue-on-error: true" in workflow

def test_daily_and_historical_workflows_share_b2_writer_lock():
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    daily = (root / "nse-download-check.yml").read_text(encoding="utf-8")
    historical = (root / "nifty-historical-backfill.yml").read_text(encoding="utf-8")

    assert "group: nse-b2-archive" in daily
    assert "group: nse-b2-archive" in historical
    assert "cancel-in-progress: false" in daily
    assert "cancel-in-progress: false" in historical



def test_range_forward_shadow_reuses_daily_nse_download_without_extra_b2_reads():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    assert 'cron: "30 14 * * 1-5"' in workflow
    assert "actions: read" in workflow
    assert "range-code/scripts/range_shadow_validation.py" in workflow
    assert "--reports-dir data/nse_reports" in workflow
    assert "nifty-range-candidate-" in workflow
    assert "steps.range-bundle.outputs.head_sha" in workflow
    assert "nifty-range-shadow-state" in workflow
    assert "previous_state_restore_failed; history was not reset" in workflow
    assert "b2_reads" in workflow
    assert "b2_writes" in workflow
    assert "continue-on-error: true" in workflow


def test_range_forward_shadow_retains_frozen_checkpoint_for_validation_window():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    assert "nifty-range-shadow-checkpoint" in workflow
    assert "checkpoint-verify" in workflow
    assert "retention-days: 90" in workflow
    assert 'assert len(features) == 494' in workflow
