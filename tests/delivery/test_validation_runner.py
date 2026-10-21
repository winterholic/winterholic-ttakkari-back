from ttakkari.delivery.validation import collected_test_count, passed_test_count


def test_pytest_summaries_require_an_explicit_nonempty_suite():
    assert collected_test_count("collected 12 items / 8 deselected / 4 selected") == 12
    assert collected_test_count("0 tests collected") == 0
    assert collected_test_count("no tests ran") == 0
    assert passed_test_count("12 passed, 3 deselected in 2.4s") == 12
    assert passed_test_count("0 passed, 12 deselected") == 0
    assert passed_test_count("no tests ran") == 0
