"""Safety checks for the operator repair tool; no network or database writes."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('repair',Path(__file__).with_name('repair-demoted-slot.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class RepairSafety(unittest.TestCase):
 def test_primary_is_never_repaired(self):
  with patch.object(r.c,'leader',return_value='s1'),patch.object(r.c,'sql') as sql,patch.object(r.c,'put') as put:
   with self.assertRaises(AssertionError):r.repair('s1')
   sql.assert_not_called();put.assert_not_called()

 def test_synced_standby_is_not_dropped(self):
  slot={'recovery':True,'active':False,'failover':True,'synced':True}
  with patch.object(r.c,'leader',return_value='s1'),patch.object(r,'state',return_value=slot),patch.object(r.c,'sql') as sql,patch.object(r.c,'put') as put:
   with self.assertRaises(AssertionError):r.repair('s3')
   sql.assert_not_called();put.assert_not_called()

 def test_resume_only_finishes_and_preserves_original_tag(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'secrets/cdc/slot-repair-s3.json';path.parent.mkdir(parents=True)
   path.write_text(json.dumps({'node':'s3','originalNofailover':False,'before':{'synced':False}}))
   with patch.object(r.c,'ROOT',Path(tmp)),patch.object(r,'finish',return_value={}) as finish,patch.object(r.c,'sql') as sql:
    r.repair('s3',resume=True)
    finish.assert_called_once_with('s3',False);sql.assert_not_called();self.assertFalse(path.exists())

 def test_failed_resume_retains_checkpoint(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'secrets/cdc/slot-repair-s3.json';path.parent.mkdir(parents=True)
   path.write_text(json.dumps({'node':'s3','originalNofailover':False,'before':{}}))
   with patch.object(r.c,'ROOT',Path(tmp)),patch.object(r,'finish',side_effect=RuntimeError('not safe')):
    with self.assertRaises(RuntimeError):r.repair('s3',resume=True)
    self.assertTrue(path.exists())

 def test_temporary_slot_never_restores_promotion(self):
  primary={'active':True,'failover':True,'temporary':False,'invalid':None}
  standby={'recovery':True,'synced':True,'failover':True,'temporary':True}
  with patch.object(r.c,'leader',return_value='s1'),patch.object(r.c,'members',return_value=[{'name':'s3','tags':{'nofailover':True}}]),patch.object(r,'state',side_effect=lambda n:primary if n=='s1' else standby),patch.object(r.time,'sleep'),patch.object(r.c,'put') as put:
   with self.assertRaises(RuntimeError):r.finish('s3',False)
   put.assert_not_called()

if __name__=='__main__':unittest.main()
