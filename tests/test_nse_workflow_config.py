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
