"""Выкладка на сервер одним SSH-подключением (провайдер режет частые подключения).

python deploy.py             обновить код, .env на сервере не трогать
python deploy.py --with-env  первая установка: отправить и локальный .env
Хост берётся из DEPLOY_HOST или ~/.ssh/config (алиас по умолчанию antispam-server).
"""
import io
import os
import subprocess
import sys
import tarfile

HOST = os.getenv("DEPLOY_HOST", "antispam-server")
ROOT = os.path.dirname(os.path.abspath(__file__))


def add_bytes(tar, name, data: bytes):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def main():
    files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            with open(os.path.join(ROOT, f), "rb") as fh:
                add_bytes(tar, "app/" + f, fh.read().replace(b"\r\n", b"\n"))
        if "--with-env" in sys.argv:
            env = os.path.join(ROOT, ".env")
            if not os.path.exists(env):
                sys.exit("Нет .env, сначала python setup_env.py")
            with open(env, "rb") as fh:
                add_bytes(tar, "app/.env", fh.read().replace(b"\r\n", b"\n"))
    remote = ("rm -rf /tmp/antispam-deploy && mkdir -p /tmp/antispam-deploy && "
              "tar -xz -C /tmp/antispam-deploy && bash /tmp/antispam-deploy/app/deploy/install.sh")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOST, remote],
                       input=buf.getvalue())
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
