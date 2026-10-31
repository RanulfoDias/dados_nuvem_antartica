from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from maestro_bridge.arduino import TextMonitor, database_path
from maestro_bridge.destination import read_watch_directory, save_watch_directory
from maestro_bridge.drive import DriveBackend
from maestro_bridge.local import Queue
from maestro_bridge.schedule import sync_scheduled
from maestro_bridge.ui import start_server
from tests.test_drive import FakeDrive


class ArduinoTests(unittest.TestCase):
    def test_txt_daily_upload_original_bytes_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.toml'
            config.write_text('[bridge]\nupload_time="12:00"\narduino_upload_time="18:00"\nacquisition_timezone="America/Sao_Paulo"\n')
            raw=b'27/10/2026;25,4\r\n27/10/2026;25,6\r\n'
            p=root/'Temperatura.TXT';p.write_bytes(raw)
            (root/'ignore.csv').write_bytes(b'ignored')
            stamp=datetime(2026,10,27,20,tzinfo=timezone.utc).timestamp()
            os.utime(p,(stamp,stamp))
            path=database_path(root/'bridge.sqlite3');queue=Queue(path)
            monitor=TextMonitor(root,queue,stable_seconds=1,acquisition_timezone='America/Sao_Paulo',selected_date='2026-10-27')
            self.assertEqual(monitor.scan(0),0)
            self.assertEqual(monitor.scan(1),1)
            service=FakeDrive()
            backend=DriveBackend(folder_id='folder1',token_file='unused',database=path,service=service,media_factory=lambda stream,**kw:stream.read(),subdirectory='sensor_arduino')
            self.assertEqual(sync_scheduled(queue,backend,config,key='arduino_upload_time',now=datetime(2026,10,27,20,59,tzinfo=timezone.utc)),0)
            self.assertEqual(sync_scheduled(queue,backend,config,key='arduino_upload_time',now=datetime(2026,10,27,21,tzinfo=timezone.utc)),1)
            objects={body['name']:(body,data) for body,data in service.objects.values()}
            self.assertEqual(set(objects),{'sensor_arduino','27_10_2026','Temperatura.TXT'})
            self.assertEqual(objects['Temperatura.TXT'][1],raw)
            self.assertEqual(objects['Temperatura.TXT'][0]['parents'],[objects['27_10_2026'][0]['id']])
            self.assertEqual(objects['27_10_2026'][0]['parents'],[objects['sensor_arduino'][0]['id']])
            queue.close();queue=Queue(path)
            self.assertEqual(sync_scheduled(queue,backend,config,key='arduino_upload_time',now=datetime(2026,10,27,22,tzinfo=timezone.utc)),0)
            queue.close()

    def test_save_arduino_folder_does_not_change_maestro(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'maestro').mkdir();(root/'arduino').mkdir()
            config=root/'config.toml';config.write_text('[bridge]\nwatch_directory="maestro"\n')
            self.assertEqual(read_watch_directory(config,'arduino_directory'),'')
            save_watch_directory(config,str(root/'arduino'),'arduino_directory')
            self.assertEqual(read_watch_directory(config),str(root/'maestro'))
            self.assertEqual(read_watch_directory(config,'arduino_directory'),str(root/'arduino'))
            with self.assertRaises(ValueError):save_watch_directory(config,str(root/'missing'),'arduino_directory')

    def test_http_save_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.toml';config.write_text('[bridge]\n')
            server,thread=start_server(root/'queue.db',0,config_path=config)
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
            try:
                connection.request('GET','/api/status');state=json.loads(connection.getresponse().read())
                headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{server.server_port}','X-CSRF-Token':state['csrf']}
                connection.request('POST','/api/arduino-directory',json.dumps({'path':str(root)}),headers)
                response=connection.getresponse();self.assertEqual(response.status,200);self.assertEqual(json.loads(response.read())['arduino_directory'],str(root))
                connection.request('POST','/api/arduino-schedule',json.dumps({'time':'19:45'}),headers)
                response=connection.getresponse();self.assertEqual(response.status,200);self.assertEqual(json.loads(response.read())['time'],'19:45')
                connection.request('GET','/api/status');state=json.loads(connection.getresponse().read())
                self.assertEqual(state['arduino_schedule']['time'],'19:45')
                self.assertEqual(state['upload_schedule']['time'],'')
                self.assertEqual(state['arduino_directory'],str(root));self.assertEqual(state['arduino']['total'],0)
            finally:
                connection.close();server.shutdown();server.server_close();thread.join()
