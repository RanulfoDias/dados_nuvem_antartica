import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from maestro_bridge.arduino import TextAcquisition
from maestro_bridge.arduino_daily import ArduinoDailyBackend, consolidate
from maestro_bridge.local import Queue
from tests.test_drive import FakeDrive, Request


class Missing(Exception):
    resp = SimpleNamespace(status=404)


class DailyDrive(FakeDrive):
    def get(self,fileId,**kw):
        if not fileId.startswith('folder') and fileId not in self.objects:
            return Request(lambda: (_ for _ in ()).throw(Missing()))
        return super().get(fileId,**kw)

    def update(self,fileId,body=None,media_body=None,**kw):
        def apply():
            metadata,data=self.objects[fileId]
            metadata.update(body or {})
            self.objects[fileId]=(metadata, data if media_body is None else media_body)
            return {'id':fileId}
        return Request(apply)


class DailyTests(unittest.TestCase):
    def test_growing_sources_use_same_file_and_preserve_distinct_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=Queue(Path(directory)/'db.sqlite')
            service=DailyDrive()
            backend=ArduinoDailyBackend(folder_id='folder1',token_file='',database=Path(directory)/'db.sqlite',service=service,media_factory=lambda f,**kw:f.read(),subdirectory='sensor_arduino')
            def send(raw,name='original.txt',day='2026-10-27'):
                sha=hashlib.sha256(raw).hexdigest()
                queue.put(TextAcquisition(sha),raw,Path(name),modified_date=day)
                backend.upload(sha,raw)
            a=b'DATA_HORA;TEMP\n27/10/2026 10:00:00;20\n'
            send(a)
            identifier=next(k for k,(b,_) in service.objects.items() if b['name']=='original.txt')
            send(a+b'27/10/2026 11:00:00;21\n')
            send(b'DATA_HORA;TEMP\n27/10/2026 11:00:00;21\n27/10/2026 11:00:00;22\n','second.txt')
            body,data=service.objects[identifier]
            self.assertEqual(body['name'],'original.txt')
            self.assertEqual(data.count(b'DATA_HORA;TEMP'),1)
            self.assertEqual(data.count(b'11:00:00;21'),1)
            self.assertIn(b'11:00:00;22',data)
            self.assertEqual(len(service.objects),3)
            send(b'DATA_HORA;TEMP\n28/10/2026 10:00:00;20\n','next.txt','2026-10-28')
            self.assertEqual(len(service.objects),5)
            queue.close()

    def test_incompatible_columns_rejected(self):
        with self.assertRaises(ValueError):
            consolidate([{'raw':b'DATA_HORA;A\n1;2\n'},{'raw':b'DATA_HORA;B\n1;3\n'}])
