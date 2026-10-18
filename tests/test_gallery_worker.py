"""Real pipe tests for reusable extraction; fixtures never access the network."""
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_gallery_client as fixtures
MEDIA, POST, SOURCE, TWEET = fixtures.MEDIA, fixtures.POST, fixtures.SOURCE, fixtures.TWEET
import gallery_client as client
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'extractor'))
import gallery_bridge as bridge


class ReusableClientTests(unittest.TestCase):
    def setUp(self):
        fixtures.GalleryClientTests.setUp(self)
        self.source.reuse_process = True

    def tearDown(self):
        self.source.close()
        fixtures.GalleryClientTests.tearDown(self)

    def script_code(self, *, wrong_id=False, private=False, hang=False):
        record = {**MEDIA,'metadata':{'auth_token':'fixture secret'}} if private else MEDIA
        return f'''import sys,json,time
print(json.dumps({{"type":"ready","protocol":1}}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 rid=r["request_id"]+{int(wrong_id)}
 if {hang!r}: time.sleep(60)
 record={record!r}
 print(json.dumps({{**record,"request_id":rid}}),flush=True)
 print(json.dumps({{"type":"done","ok":True,"count":1,"request_id":rid}}),flush=True)
'''

    def test_two_complete_sources_share_one_process_with_separate_frames(self):
        self.script.write_text(self.script_code(),encoding='utf-8')
        real_popen = subprocess.Popen
        with patch.object(client.GallerySource,'_command',return_value=[sys.executable,str(self.script)]), \
                patch('gallery_worker.subprocess.Popen',wraps=real_popen) as spawn:
            first = list(self.source.iter_media(SOURCE))
            worker = self.source._worker.process
            second = list(self.source.iter_media(TWEET))
            self.assertEqual(first,second)
            self.assertEqual(first,[(MEDIA['url'],MEDIA['metadata'])])
            self.assertEqual(spawn.call_count,1)
            self.assertIs(self.source._worker.process,worker)
            self.assertIsNone(worker.poll())
            self.source.close()
            self.assertIsNotNone(worker.poll())

    def test_early_close_discards_process_and_next_source_starts_fresh(self):
        self.script.write_text(self.script_code(),encoding='utf-8')
        with patch.object(client.GallerySource,'_command',return_value=[sys.executable,str(self.script)]):
            iterator = self.source.iter_media(SOURCE)
            next(iterator)
            process = self.source._worker.process
            iterator.close()
            self.assertIsNone(self.source._worker)
            self.assertIsNotNone(process.poll())
            self.assertEqual(len(list(self.source.iter_media(TWEET))),1)

    def test_cancel_blocked_worker_then_restart_has_no_stale_response(self):
        self.script.write_text(self.script_code(hang=True),encoding='utf-8')
        timer = threading.Timer(0.4,self.event.set)
        with patch.object(client.GallerySource,'_command',return_value=[sys.executable,str(self.script)]):
            timer.start()
            started=time.monotonic()
            try:
                with self.assertRaises(client.Cancelled):
                    list(self.source.iter_media(SOURCE))
            finally:
                timer.cancel()
            self.assertLess(time.monotonic()-started,5)
            self.assertIsNone(self.source._worker)
            self.event.clear()
            self.script.write_text(self.script_code(),encoding='utf-8')
            self.assertEqual(len(list(self.source.iter_media(SOURCE))),1)

    def test_wrong_source_id_and_private_metadata_are_rejected_and_worker_closed(self):
        for kwargs in ({'wrong_id':True},{'private':True}):
            with self.subTest(kwargs=kwargs), patch.object(client.GallerySource,'_command',return_value=[sys.executable,str(self.script)]):
                self.script.write_text(self.script_code(**kwargs),encoding='utf-8')
                with self.assertRaises(client.GallerySourceError):
                    list(self.source.iter_media(SOURCE))
                self.assertIsNone(self.source._worker)
                self.assertNotIn('fixture secret',' '.join(self.logs))

    def test_dead_peer_with_buffered_input_does_not_break_cleanup(self):
        self.script.write_text(self.script_code(),encoding='utf-8')
        with patch.object(client.GallerySource,'_command',return_value=[sys.executable,str(self.script)]):
            list(self.source.iter_media(SOURCE))
            process=self.source._worker.process
            process.kill()
            process.wait(timeout=2)
            process.stdin.write(b'buffered command')
            self.source.close()
            self.assertIsNone(self.source._worker)


class ServerTests(unittest.TestCase):
    def serve(self, requests):
        lines=b''.join(json.dumps(r).encode()+b'\n' for r in requests)
        records=[]
        def media_rows(url):
            yield MEDIA['url'], MEDIA['metadata']
        def thread_rows(url):
            yield None, POST['metadata']
        source=SimpleNamespace(iter_media=media_rows,iter_thread=thread_rows)
        with patch.object(bridge.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(lines))), \
                patch.object(bridge,'_emit',side_effect=records.append), \
                patch.object(bridge,'GallerySource',return_value=source) as factory:
            result=bridge.serve(Path('fixture.cookies'),threading.Event())
        return result,records,factory.call_count

    def test_media_and_thread_frames_have_matching_ids_and_counts(self):
        result,rows,count=self.serve([{'request_id':1,'url':SOURCE,'conversation':False},
                                     {'request_id':2,'url':TWEET,'conversation':True}])
        self.assertEqual(result,0)
        self.assertEqual(rows[0],{'type':'ready','protocol':1})
        self.assertEqual([r['request_id'] for r in rows[1:]],[1,1,2,2])
        self.assertEqual([r['type'] for r in rows[1:]],['metadata','done','post','done'])
        self.assertEqual(count,2)

    def test_invalid_source_mode_and_sequence_are_rejected(self):
        for requests in ([{'request_id':1,'url':'https://outside.invalid/','conversation':False}],
                         [{'request_id':1,'url':SOURCE,'conversation':True}],
                         [{'request_id':True,'url':SOURCE,'conversation':False}],
                         [{'request_id':1,'url':SOURCE,'conversation':False}]*2):
            with self.subTest(requests=requests), self.assertRaises(ValueError):
                self.serve(requests)
