from pathlib import Path


def test_docker_build_records_expire_after_seven_days():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    assert '\nenv:\n  DOCKER_BUILD_RECORD_RETENTION_DAYS: "7"\n' in workflow
    assert workflow.count("DOCKER_BUILD_RECORD_RETENTION_DAYS") == 1
