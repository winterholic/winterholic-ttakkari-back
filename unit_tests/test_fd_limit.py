import resource

from ttakkari import fd_limit


def test_raises_soft_limit_and_children_inherit_it():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        applied = fd_limit.raise_nofile_limit()
        assert applied > 256
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == applied
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_never_lowers_an_already_higher_limit():
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert fd_limit.raise_nofile_limit(target=16) == soft
