"""Fencing and ordering properties; no real DCS/PG writes."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('guard',Path(__file__).with_name('ha-guard.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def slot(**changes):
    value=dict(failover=True,temporary=False,invalid=None,database='crawler',plugin='pgoutput',wal_status='reserved',confirmed='0/20',restart='0/10',synced=True,active=False)
    return {**value,**changes}

def standby(**changes):
    return {**dict(recovery=True,replay='0/30',sender=m.NODES['s1'],slot=slot(),streaming_from_current_primary=True),**changes}

def snapshot(owner='s1',allowed=('s2','s3')):
    return {'leader':{'value':owner,'revision':'10'},'cdc_guard':{'value':'opaque','revision':'11'},'initialize':{'value':'123'},
        'members':{n:{} for n in m.NODES},'policy':{'version':1,'owner':owner,'allowed':list(allowed),'floor':'0/20','system_id':'123','generation':'old'}}

def guard(name='s1'):
    g=m.Guard.__new__(m.Guard);g.name=name;g.last_tick=0
    g.dcs=Mock();g.dcs.snapshot.return_value=snapshot();g.dcs.publish.return_value=True
    g.barrier=Mock(return_value=set(m.NODES)-{name});g.set_barrier=Mock()
    g.query=Mock(side_effect=lambda sql,*args,**kwargs:[('123',)] if 'system_identifier' in sql else [('0/20',)])
    g.progress_pending=Mock();g.state=Mock(return_value=standby());g.peers=Mock(return_value={'s2':standby(),'s3':standby()})
    return g

class FencingTests(unittest.TestCase):
    def test_exclusion_must_commit_before_reducing_barrier(self):
        g=guard();g.peers.return_value={'s2':standby(),'s3':None};order=[]
        g.set_barrier.side_effect=lambda names:order.append(('barrier',set(names)))
        g.dcs.publish.side_effect=lambda *args:order.append(('policy',set(args[-1]['allowed']))) or True
        g.primary(snapshot(),{'slot':slot()})
        self.assertEqual(order,[('barrier',{'s2','s3'}),('policy',{'s2'}),('barrier',{'s2'})])

    def test_admission_expands_barrier_before_cas(self):
        g=guard();g.barrier.return_value={'s2'};order=[]
        g.set_barrier.side_effect=lambda names:order.append(('barrier',set(names)))
        g.dcs.publish.side_effect=lambda *args:order.append(('policy',set(args[-1]['allowed']))) or True
        g.primary(snapshot(allowed=('s2',)),{'slot':slot()})
        self.assertEqual(order[:2],[('barrier',{'s2','s3'}),('policy',{'s2','s3'})])

    def test_failed_cas_does_not_reduce_barrier(self):
        g=guard();g.peers.return_value={'s2':standby(),'s3':None};g.dcs.publish.return_value=False
        with self.assertRaises(RuntimeError):g.primary(snapshot(),{'slot':slot()})
        g.set_barrier.assert_called_once_with({'s2','s3'})

    def test_unreplayed_floor_cannot_admit_candidate(self):
        g=guard();g.barrier.return_value={'s2'};g.peers.return_value['s3']['replay']='0/1'
        self.assertEqual(g.primary(snapshot(allowed=('s2',)),{'slot':slot()})['state'],'WAITING_FOR_ADMISSION_WAL')
        g.dcs.publish.assert_not_called()

    def test_no_healthy_peer_never_disables_barrier(self):
        g=guard();g.peers.return_value={'s2':None,'s3':None}
        self.assertEqual(g.primary(snapshot(),{'slot':slot()})['state'],'WAITING_FOR_SAFE_STANDBY')
        g.dcs.publish.assert_not_called();g.set_barrier.assert_called_once_with({'s2','s3'})

    def test_old_primary_cannot_publish(self):
        g=guard()
        with self.assertRaises(RuntimeError):g.primary(snapshot(owner='s2'),{'slot':slot()})
        g.dcs.publish.assert_not_called();g.set_barrier.assert_not_called()

    def test_operator_exclusion_is_respected(self):
        g=guard();s=snapshot();s['members']['s3']={'tags':{'nofailover':True}}
        g.primary(s,{'slot':slot()})
        self.assertEqual(g.dcs.publish.call_args.args[-1]['allowed'],['s2'])

    def test_promotion_requires_policy_and_slot_safety(self):
        for mode in ['excluded','missing_policy','temporary','invalid','unsynced','wrong_system','behind','no_leader']:
            with self.subTest(mode=mode):
                g=guard('s2');s=snapshot(owner='s2');s['policy']['owner']='s1';state=standby()
                if mode=='excluded':s['policy']['allowed']=['s3']
                if mode=='missing_policy':s['policy']=None
                if mode=='temporary':state['slot']['temporary']=True
                if mode=='invalid':state['slot']['invalid']='wal_removed'
                if mode=='unsynced':state['slot']['synced']=False
                if mode=='wrong_system':s['initialize']['value']='456'
                if mode=='behind':state['replay']='0/1'
                if mode=='no_leader':s['leader']['value']='s1'
                g.dcs.snapshot.return_value=s;g.state.return_value=state
                with self.assertRaises(RuntimeError):g.pre_promote()
                g.set_barrier.assert_not_called()

    def test_valid_promotion_resets_barrier(self):
        g=guard('s2');s=snapshot(owner='s2');s['policy']['owner']='s1';g.dcs.snapshot.return_value=s
        g.pre_promote();g.set_barrier.assert_called_once_with({'s1','s3'})

    def test_repair_never_drops_synced_slot(self):
        g=guard('s3');g.standby(snapshot(),standby())
        g.query.assert_not_called()

    def test_repair_requires_exclusion_and_upstream_progress(self):
        for mode in ['included','upstream_behind','active_local','not_current_upstream','changed_policy']:
            with self.subTest(mode=mode):
                g=guard('s3');s=snapshot(allowed=('s2',));local=standby(slot=slot(synced=False))
                upstream=standby(recovery=False,slot=slot(confirmed='0/30'))
                if mode=='included':s['policy']['allowed'].append('s3')
                if mode=='upstream_behind':upstream['slot']['confirmed']='0/1'
                if mode=='active_local':local['slot']['active']=True
                if mode=='not_current_upstream':local['sender']=m.NODES['s2']
                latest=copy.deepcopy(s)
                if mode=='changed_policy':latest['cdc_guard']['revision']='12'
                g.state.return_value=upstream;g.dcs.snapshot.return_value=latest
                with self.assertRaises(RuntimeError):g.standby(s,local)
                self.assertFalse(any('pg_drop' in call.args[0] for call in g.query.call_args_list))

    def test_stale_inactive_standby_slot_can_be_repaired(self):
        g=guard('s3');s=snapshot(allowed=('s2',));g.dcs.snapshot.return_value=s
        g.state.return_value=standby(recovery=False,slot=slot(confirmed='0/30'))
        g.standby(s,standby(slot=slot(synced=False)))
        drops=[call for call in g.query.call_args_list if 'pg_drop' in call.args[0]]
        self.assertEqual(len(drops),1);self.assertIn('pg_is_in_recovery()',drops[0].args[0])

    def test_cas_fences_leader_and_policy_revisions(self):
        dcs=m.DCS.__new__(m.DCS);dcs.rpc=Mock(return_value={'succeeded':False})
        self.assertFalse(dcs.publish('s1',snapshot(),{'allowed':['s2']}))
        compares=dcs.rpc.call_args.args[1]['compare']
        self.assertEqual([c['target'] for c in compares],['VALUE','MOD','MOD'])

if __name__=='__main__':unittest.main()
