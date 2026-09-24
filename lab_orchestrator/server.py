"""Loopback-only HTTP adapter for the local simulation console."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit,unquote
from .controls import ConflictError
from .service import PanelService,BatchNotFoundError

WEB=Path(__file__).with_name('web')


def create_server(service,port=8765):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,status,data,content_type='application/json; charset=utf-8'):
            body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def valid_host(self):
            port=self.server.server_address[1]
            return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')

        def do_GET(self):
            if not self.valid_host():return self.send(403,{'error':'Invalid local host'})
            path=urlsplit(self.path).path
            try:
                if path=='/api/v1/bootstrap':return self.send(200,{**service.call('bootstrap'),'control_token':token})
                if path=='/api/v1/state':return self.send(200,service.call('state'))
                if path=='/api/v1/history':return self.send(200,service.call('history'))
                if path=='/api/v1/drivers':return self.send(200,service.call('drivers'))
                if path=='/favicon.ico':return self.send(204,b'','image/x-icon')
                static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
                if path in static:
                    file,mime=static[path];return self.send(200,(WEB/file).read_bytes(),mime)
                self.send(404,{'error':'Not found'})
            except Exception as exc:self.send(500,{'error':str(exc)})

        def do_POST(self):
            port=self.server.server_address[1]
            origin=self.headers.get('Origin')
            allowed=(f'http://127.0.0.1:{port}',f'http://localhost:{port}')
            if (not self.valid_host() or (origin is not None and origin not in allowed)
                    or not secrets.compare_digest(self.headers.get('X-Control-Token',''),token)):
                return self.send(403,{'error':'Local control token or origin is invalid'})
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self.send(415,{'error':'JSON request body required'})
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:return self.send(400,{'error':'Invalid content length'})
            if length<=0 or length>1024*1024:return self.send(413,{'error':'Request body must be 1 byte to 1 MiB'})
            try:
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):raise ValueError('Body must be a JSON object')
                parts=[unquote(p) for p in urlsplit(self.path).path.split('/') if p]
                if parts==['api','v1','runs']:
                    if set(payload)-{'actor','key','scenario','workflow'}:raise ValueError('Unsupported submission fields')
                    return self.send(201,service.call('create',**payload))
                if type(payload.get('expected_version')) is not int:raise ValueError('expected_version is required')
                if parts[:3]==['api','v1','runs'] and len(parts)==5 and parts[4] in ('pause','resume','cancel','recover'):
                    allowed_fields={'actor','expected_version'}|({'reason'} if parts[4]=='cancel' else set())
                    if set(payload)-allowed_fields:raise ValueError('Unsupported control fields')
                    return self.send(200,service.call(parts[4],run_id=parts[3],**payload))
                if parts[:3]==['api','v1','alarms'] and len(parts)==5 and parts[4]=='acknowledge':
                    if set(payload)-{'actor','expected_version'}:raise ValueError('Unsupported acknowledgement fields')
                    return self.send(200,service.call('acknowledge_alarm',command_id=parts[3],**payload))
                if parts[:3]==['api','v1','recoveries'] and len(parts)==5 and parts[4]=='resolve':
                    if set(payload)-{'actor','expected_version','decision','reason','evidence'}:raise ValueError('Unsupported recovery fields')
                    return self.send(200,service.call('resolve',command_id=parts[3],**payload))
                self.send(404,{'error':'Not found'})
            except BatchNotFoundError as exc:self.send(404,{'error':str(exc)})
            except ConflictError as exc:self.send(409,{'error':str(exc)})
            except (ValueError,TypeError,UnicodeError) as exc:self.send(422,{'error':str(exc)})
            except Exception as exc:self.send(500,{'error':str(exc)})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Local laboratory simulation control console')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data',default='.runtime/console')
    args=parser.parse_args()
    service=PanelService(args.data)
    server=None
    try:
        server=create_server(service,args.port)
        print(f'Console: http://127.0.0.1:{server.server_address[1]}',flush=True)
        server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if server:server.server_close()
        service.close()


if __name__=='__main__':main()
