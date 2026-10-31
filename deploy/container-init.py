"""s6 supervision with immutable run scripts and fixed process identities."""
import os
from pathlib import Path


def directory(path, uid, gid, mode):
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError('Unexpected mount symlink')
    # Take temporary ownership so CAP_FOWNER is unnecessary on later wakes.
    os.chown(target, 0, gid)
    os.chmod(target, mode)
    os.chown(target, uid, gid)


for path, uid, gid, mode in [('/data/app', 31001, 31000, 0o700),
                            ('/data/files', 31001, 31000, 0o700),
                            ('/data/browser', 31003, 31000, 0o700),
                            ('/run/personal-bot', 0, 31000, 0o1770),
                            # The isolated child has GID 31004 and no shared
                            # groups. It needs traversal to its private job,
                            # but no role needs to list/write this parent.
                            ('/scratch', 0, 31000, 0o711),
                            ('/scratch/outputs', 31001, 31000, 0o700),
                            ('/scratch/jobs', 0, 31000, 0o711)]:
    directory(path, uid, gid, mode)
services = Path('/run/services')
services.mkdir(mode=0o755, exist_ok=True)
for name in ['api', 'worker', 'browser', 'executor']:
    folder = services / name
    folder.mkdir(exist_ok=True)
    # /run may remain noexec; scripts execute from the immutable image mount.
    (folder / 'run').symlink_to(f'/opt/personal-bot/services/{name}/run')
    (folder / 'finish').symlink_to('/opt/personal-bot/services/finish')
os.execvp('s6-svscan', ['s6-svscan', '/run/services'])
