# RT-381 temporary: proves the CI test step turns red on a failing test. Removed in the next commit.


def test_ci_red_proof():
    raise AssertionError("RT-381 deliberate failure")
