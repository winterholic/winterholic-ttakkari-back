from __future__ import annotations

import pytest


async def _create(client, auth, path, name="ws", **extra):
    return await client.post("/api/workspaces", json={"name": name, "root_path": str(path), **extra}, headers=auth)


async def test_create_ok_and_get_and_list(client, auth, make_dir):
    d = make_dir()
    r = await _create(client, auth, d)
    assert r.status_code == 201
    ws = r.json()
    assert ws["root_path"] == str(d.resolve()) and ws["is_git"] is False and ws["export_allowed"] is True
    assert (await client.get(f"/api/workspaces/{ws['id']}", headers=auth)).json()["id"] == ws["id"]
    assert [w["id"] for w in (await client.get("/api/workspaces", headers=auth)).json()] == [ws["id"]]


async def test_create_git_workspace_sets_is_git(client, auth, make_git_dir):
    r = await _create(client, auth, make_git_dir())
    assert r.json()["is_git"] is True


async def test_allowed_root_itself_is_accepted(client, auth):
    from conftest import ALLOWED
    assert (await _create(client, auth, ALLOWED)).status_code == 201


async def test_create_outside_allowed_roots_denied(client, auth, outside_dir):
    r = await _create(client, auth, outside_dir)
    assert r.status_code == 403
    assert r.json()["code"] == "outside_root"


async def test_create_nonexistent_path_404(client, auth):
    from conftest import ALLOWED
    r = await _create(client, auth, ALLOWED / "nope")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_create_file_path_rejected(client, auth, make_dir):
    f = make_dir() / "f.txt"
    f.write_text("x")
    r = await _create(client, auth, f)
    assert r.status_code == 400 and r.json()["code"] == "not_a_dir"


async def test_create_relative_path_rejected(client, auth):
    r = await _create(client, auth, "relative/dir")
    assert r.status_code == 400 and r.json()["code"] == "bad_path"


async def test_create_symlink_escaping_allowed_roots_denied(client, auth, make_dir, outside_dir):
    link = make_dir() / "link"
    link.symlink_to(outside_dir)
    r = await _create(client, auth, link)
    assert r.status_code == 403


async def test_create_sensitive_dir_denied(client, auth, make_dir):
    d = make_dir() / ".ssh"
    d.mkdir()
    r = await _create(client, auth, d)
    assert r.status_code == 403 and r.json()["code"] == "sensitive"


async def test_duplicate_path_409(client, auth, make_dir):
    d = make_dir()
    assert (await _create(client, auth, d)).status_code == 201
    r = await _create(client, auth, d, name="again")
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    # 심볼릭 링크로 같은 실제 경로를 다시 등록해도 중복이다.
    alias = d.parent / "alias-of-dup"
    alias.symlink_to(d)
    assert (await _create(client, auth, alias, name="alias")).status_code == 409


async def test_patch_archive_and_list_filter(client, auth, make_dir):
    ws = (await _create(client, auth, make_dir())).json()
    r = await client.patch(f"/api/workspaces/{ws['id']}", json={"archived": True, "name": "renamed"}, headers=auth)
    assert r.status_code == 200 and r.json()["archived_at"] and r.json()["name"] == "renamed"
    assert (await client.get("/api/workspaces", headers=auth)).json() == []
    assert len((await client.get("/api/workspaces", params={"include_archived": "true"}, headers=auth)).json()) == 1
    # 보관된 워크스페이스에는 세션을 만들 수 없다.
    r = await client.post("/api/sessions", json={"workspace_id": ws["id"]}, headers=auth)
    assert r.status_code == 409


async def test_get_unknown_workspace_404(client, auth):
    r = await client.get("/api/workspaces/00000000-0000-0000-0000-000000000000", headers=auth)
    assert r.status_code == 404


@pytest.fixture
async def tree(client, auth, make_dir, outside_dir):
    d = make_dir()
    (d / "docs").mkdir()
    (d / "docs" / "guide.md").write_text("g")
    (d / "b.txt").write_text("bb")
    (d / "A.txt").write_text("a")
    (d / ".hidden").write_text("h")
    (d / ".env").write_text("SECRET=1")
    (d / ".git").mkdir()
    (outside_dir / "secret.txt").write_text("outside")
    (d / "escape").symlink_to(outside_dir)
    (d / "inside-link").symlink_to(d / "docs")
    ws = (await _create(client, auth, d)).json()
    return ws, d, outside_dir


async def test_list_files_root_filters_hidden_and_sorts(client, auth, tree):
    ws, _d, _ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", headers=auth)
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert not any(n.startswith(".") for n in names)
    # 디렉터리(심볼릭 링크 제외) 먼저, 그다음 대소문자 무시 이름순
    assert names[0] == "docs"
    assert names[1:] == sorted(names[1:], key=str.lower)
    types = {e["name"]: e["type"] for e in body["entries"]}
    assert types["docs"] == "dir" and types["b.txt"] == "file" and types["escape"] == "symlink"
    assert body["rel_path"] == "." and body["truncated"] is False


async def test_list_files_show_hidden_marks_sensitive(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"show_hidden": "true"}, headers=auth)
    by = {e["name"]: e for e in r.json()["entries"]}
    assert ".hidden" in by and by[".env"]["sensitive"] is True and by[".hidden"]["sensitive"] is False


async def test_list_files_subdir(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": "docs"}, headers=auth)
    assert [e["rel_path"] for e in r.json()["entries"]] == ["docs/guide.md"]
    assert r.json()["rel_path"] == "docs"


@pytest.mark.parametrize("bad", ["..", "../x", "docs/../../x", "/etc", "docs/..", "a\x00b"])
async def test_list_files_rejects_traversal(client, auth, tree, bad):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": bad}, headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "bad_path"


async def test_list_files_rejects_symlink_escape(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": "escape"}, headers=auth)
    assert r.status_code == 403 and r.json()["code"] == "outside_root"


async def test_list_files_allows_symlink_inside_root(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": "inside-link"}, headers=auth)
    assert r.status_code == 200 and [e["name"] for e in r.json()["entries"]] == ["guide.md"]


async def test_list_files_on_file_is_400_and_missing_404(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": "b.txt"}, headers=auth)
    assert r.status_code == 400
    r = await client.get(f"/api/workspaces/{ws['id']}/files", params={"path": "nothing"}, headers=auth)
    assert r.status_code == 404


async def test_search_matches_name_case_insensitive(client, auth, tree):
    ws, d, _ = tree
    (d / "docs" / "deep").mkdir()
    (d / "docs" / "deep" / "Guide2.MD").write_text("x")
    r = await client.get(f"/api/workspaces/{ws['id']}/search", params={"q": "guide"}, headers=auth)
    assert r.status_code == 200
    assert sorted(e["rel_path"] for e in r.json()) == ["docs/deep/Guide2.MD", "docs/guide.md"]


async def test_search_skips_hidden_and_skip_dirs_and_symlinks(client, auth, tree):
    ws, d, outside = tree
    (d / "node_modules").mkdir()
    (d / "node_modules" / "needle.js").write_text("x")
    (d / ".cache2").mkdir()
    (d / ".cache2" / "needle.txt").write_text("x")
    (outside / "needle.txt").write_text("x")
    r = await client.get(f"/api/workspaces/{ws['id']}/search", params={"q": "needle"}, headers=auth)
    # 숨김·SKIP_DIRS·심링크 디렉터리(escape -> outside) 안은 검색되지 않는다.
    assert r.json() == []


async def test_search_requires_query(client, auth, tree):
    ws, *_ = tree
    r = await client.get(f"/api/workspaces/{ws['id']}/search", params={"q": ""}, headers=auth)
    assert r.status_code == 422


async def test_search_limit_200(client, auth, make_dir):
    d = make_dir()
    for i in range(205):
        (d / f"m{i}.txt").write_text("")
    ws = (await _create(client, auth, d)).json()
    r = await client.get(f"/api/workspaces/{ws['id']}/search", params={"q": "m"}, headers=auth)
    assert len(r.json()) == 200
