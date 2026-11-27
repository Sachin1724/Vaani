"""Updater: an Android beta marked "latest" on GitHub must not hide the newest Windows release."""
from vaani.engine.updater import windows_release


def test_skips_releases_without_a_windows_installer():
    releases = [
        {"tag_name": "android-v0.2.0-beta", "assets": [{"name": "Vaani-android-beta-0.2.0.apk"}]},
        {"tag_name": "v1.7.0-rc", "prerelease": True, "assets": [{"name": "Vaani-Setup.exe"}]},
        {"tag_name": "v1.6.0", "assets": [{"name": "Vaani-Setup.exe"}]},
    ]
    assert windows_release(releases)["tag_name"] == "v1.6.0"
    assert windows_release([]) == {}
