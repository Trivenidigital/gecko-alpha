from gecko.numeric import as_int, finite, num


def test_num_accepts_numbers_and_numeric_strings():
    assert num(2) == 2.0 and num("1.5") == 1.5 and num(" 3 ") == 3.0


def test_num_rejects_bool_non_finite_and_junk():
    for v in (
        True,
        False,
        float("nan"),
        float("inf"),
        "nan",
        "-inf",
        "",
        "x",
        None,
        [1],
    ):
        assert num(v) is None, v


def test_as_int_and_finite():
    assert as_int("7.9") == 7 and as_int(True) is None and as_int("x") is None
    assert finite(1) and finite(2.5)
    assert not finite(True) and not finite("1") and not finite(float("nan"))
