from app import config, settings


def test_status_reports_unconfigured_by_default(client, monkeypatch):
    # The autouse fixture defaults config.MEDIA_DIRS to [media_dir] for
    # convenience across the rest of the suite -- override it here to
    # actually test the true "nothing configured yet" state.
    monkeypatch.setattr(config, "MEDIA_DIRS", [])

    res = client.get("/api/setup/status")

    assert res.json() == {"configured": False}


def test_status_reports_configured_after_saving(client, media_dir):
    settings.set_media_dirs([media_dir])

    res = client.get("/api/setup/status")

    assert res.json() == {"configured": True}


def test_browse_lists_subdirectories(client, tmp_path):
    root = tmp_path / "browse_root"
    (root / "Movies").mkdir(parents=True)
    (root / "Music").mkdir()
    (root / ".hidden").mkdir()
    (root / "not_a_dir.txt").write_text("x")

    res = client.get("/api/setup/browse", params={"path": str(root)})

    body = res.json()
    assert body["path"] == str(root)
    assert body["parent"] == str(root.parent)
    assert body["directories"] == ["Movies", "Music"]  # sorted, hidden/files excluded


def test_browse_rejects_a_non_directory_path(client, tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")

    res = client.get("/api/setup/browse", params={"path": str(f)})

    assert res.status_code == 400


def test_browse_defaults_to_a_sensible_start_path_when_no_path_given(client):
    res = client.get("/api/setup/browse")

    assert res.status_code == 200
    assert res.json()["path"]  # non-empty, exact value is platform-dependent


def test_save_setup_requires_at_least_one_folder(client):
    res = client.post("/api/setup", json={"media_dirs": []})
    assert res.status_code == 400


def test_save_setup_rejects_a_folder_that_does_not_exist(client, tmp_path):
    res = client.post("/api/setup", json={"media_dirs": [str(tmp_path / "nope")]})
    assert res.status_code == 400


def test_save_setup_persists_folders_and_triggers_a_scan(client, media_dir, make_file):
    make_file("song.mp3", b"a")

    res = client.post("/api/setup", json={"media_dirs": [str(media_dir)]})

    assert res.status_code == 200
    assert settings.get_media_dirs() == [media_dir]
    # TestClient runs BackgroundTasks to completion before returning, so the
    # triggered scan has already finished by this point.
    assert client.get("/api/library").json()["total"] == 1


def test_status_reflects_setup_result(client, media_dir, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIRS", [])
    assert client.get("/api/setup/status").json() == {"configured": False}

    client.post("/api/setup", json={"media_dirs": [str(media_dir)]})

    assert client.get("/api/setup/status").json() == {"configured": True}
