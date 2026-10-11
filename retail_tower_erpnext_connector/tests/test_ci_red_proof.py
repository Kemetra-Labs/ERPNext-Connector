# RT-381 temporary: proves the CI job turns red on a failing test. Reverted in the next commit.


def test_ci_red_proof():
    assert False, "RT-381 deliberate failure"
