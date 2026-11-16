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
    assert "vars.ENABLE_DAILY_B2_ARCHIVE == 'true'" in workflow

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


def test_daily_range_shadow_reuses_existing_nse_reports_and_zero_b2_shadow_policy():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "nse-download-check.yml").read_text(encoding="utf-8")
    shadow = (root / "scripts" / "range_shadow_validation.py").read_text(encoding="utf-8")

    assert "nifty-range-candidate-" in workflow
    assert "nifty-range-shadow-state" in workflow
    assert "data/nse_reports" in workflow
    assert "--checkpoint data/historical_audit/checkpoint.json" in workflow
    assert "--reports-dir data/nse_reports" in workflow
    assert "continue-on-error: true" in workflow
    assert '"b2_reads": 0' in shadow
    assert '"b2_writes": 0' in shadow
    assert "MAX_NEXT_SESSION_GAP_DAYS = 4" in shadow
    assert "same_session_idempotent" in shadow


def test_range_candidate_workflow_builds_bundle_only_after_cv_pass_and_locks_promotion():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "nifty-range-only-tuning.yml").read_text(encoding="utf-8")

    assert 'assert report["range"]["cv_beats_baseline"] is True' in workflow
    assert "scripts/build_nifty_range_bundle.py" in workflow
    assert "nifty-range-candidate-" in workflow
    assert 'assert manifest["forward_shadow_validation_required"] is True' in workflow
    assert 'assert manifest["deployment_ready"] is False' in workflow
    assert 'assert manifest["production_promotion_allowed"] is False' in workflow


def test_daily_range_shadow_github_script_template_literals_are_valid():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    # The GitHub Script action executes these as JavaScript. A stray backslash
    # before the template delimiter or interpolation marker breaks parsing.
    assert chr(92) + "`" not in workflow
    assert chr(92) + "${" not in workflow
    assert "item.name === `nifty-range-candidate-${run.id}`" in workflow
    assert "core.info(`Using Range candidate artifact from run ${run.id}`)" in workflow


def test_range_only_workflow_smoke_tests_the_real_candidate_bundle():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nifty-range-only-tuning.yml"
    ).read_text(encoding="utf-8")

    assert "Smoke-test fitted Range bundle on latest verified archive features" in workflow
    assert "predict_range_bundle(bundle, features)" in workflow
    assert "deployment_ready" in workflow



def test_daily_range_shadow_never_resets_history_when_state_restore_fails():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    assert "RANGE_STATE_LOOKUP_OUTCOME" in workflow
    assert "RANGE_STATE_DOWNLOAD_OUTCOME" in workflow
    assert "previous_state_restore_failed; history was not reset" in workflow
    assert "steps.range-state-init.outputs.ready == 'true'" in workflow
    assert "actions: read" in workflow


def test_13_to_16_retrospective_is_scoped_to_its_target_date():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "nse-download-check.yml"
    ).read_text(encoding="utf-8")

    download = workflow.split(
        "- name: Download aligned 13-Nov NSE reports for retrospective check", 1
    )[1].split("\n      - name:", 1)[0]
    scoring = workflow.split(
        "- name: Generate 13-Nov forecast and score against 16-Nov actual", 1
    )[1].split("\n      - name:", 1)[0]

    assert 'date -u +%F' in download
    assert '"2026-11-16"' in download
    assert 'echo "execute_retrospective=true" >> "$GITHUB_OUTPUT"' in download
    assert "outputs.execute_retrospective == 'true'" in scoring
