import importlib.util,unittest
from datetime import datetime,timezone
from pathlib import Path
s=importlib.util.spec_from_file_location('retention',Path(__file__).with_name('retention.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class RetentionTests(unittest.TestCase):
 def test_only_expired_hours_at_normal_capacity(self):
  self.assertEqual(m.choose([{'partition':'2026091601','bytes':100},{'partition':'2026092001','bytes':100}],0.5,datetime(2026,9,21,tzinfo=timezone.utc)),['2026091601'])
 def test_capacity_drops_oldest_to_headroom(self):
  parts=[{'partition':'202609200'+str(i),'bytes':600*1024**2} for i in range(4)]
  self.assertEqual(m.choose(parts,0.5,datetime(2026,9,21,tzinfo=timezone.utc)),['2026092000','2026092001'])
 def test_invalid_partition_cannot_become_sql(self):
  with self.assertRaises(AssertionError):m.choose([{'partition':"x'; DROP DATABASE crawler_analytics;",'bytes':1}],0.1,datetime.now(timezone.utc))
 def test_low_disk_only_selects_logs(self):
  self.assertEqual(m.choose([{'partition':'2026092001','bytes':1}],0.1,datetime(2026,9,21,tzinfo=timezone.utc)),['2026092001'])
if __name__=='__main__':unittest.main()
