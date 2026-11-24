"""Local launcher: hold the bound socket through Uvicorn startup to avoid port races."""
import argparse
import asyncio
import socket
import threading
import time
import webbrowser
import logging

from .config import Settings
from .db import Database


def bind_local(port):
    last = None
    # Windows can reserve a whole port range. The OS-selected port is the final fallback.
    choices = [port, *range(port+1, min(port+10, 65535)+1), 0] if port else [0]
    for candidate in choices:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', candidate))
            sock.listen(128)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise RuntimeError('로컬 포트를 열 수 없습니다. 보안 프로그램의 로컬 연결 차단 여부를 확인하세요') from last


def open_when_ready(url):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            with opener.open(url+'/health', timeout=.3) as response:
                if response.status == 200:
                    webbrowser.open(url+'/docs')
                    return
        except (OSError, TimeoutError):
            time.sleep(.1)


def main():
    parser = argparse.ArgumentParser(description='DataCenter BE2 API 개발 실행 도구')
    parser.add_argument('command', choices=['init','serve','worker'], nargs='?', default='serve')
    parser.add_argument('--port', type=int, default=8010)
    parser.add_argument('--open', action='store_true', help='준비 후 브라우저 열기')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    settings = Settings()
    database = Database(settings.database_url)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    try:
        if args.command == 'init':
            database.migrate()
            print('Database schema is ready.')
        elif args.command == 'worker':
            from .worker import consume
            database.check_schema()
            asyncio.run(consume(database, settings))
        else:
            from .api import create_app
            import uvicorn
            # Local single-process convenience. Split API/worker setups run init first.
            database.migrate()
            sock = bind_local(args.port)
            url = f'http://127.0.0.1:{sock.getsockname()[1]}'
            print(f'\nDataCenter BE2 API: {url}\nAPI docs: {url}/docs\n', flush=True)
            if args.open:
                threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
            try:
                server = uvicorn.Server(uvicorn.Config(create_app(settings, database), log_level='info'))
                server.run(sockets=[sock])
            finally:
                sock.close()
    except KeyboardInterrupt:
        pass
    finally:
        database.engine.dispose()


if __name__ == '__main__':
    main()
