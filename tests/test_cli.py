import asyncio
from types import SimpleNamespace
import pytest
from pocketdeploy import cli
from pocketdeploy.common import DeployError


def test_dag_stops_before_host_after_uncertain_compute():
    calls=[]
    class Host:
        def prepare_keys(self):calls.append('keys');return 'public'
        def cloud_init(self):return 'secret-init'
        def bootstrap(self,*args):calls.append('host')
    class Cloud:
        def converge(self,*args):calls.append('compute');raise DeployError('uncertain outcome')
    config={}
    with pytest.raises(DeployError,match='uncertain'):
        asyncio.run(cli.converge(config,None,Cloud(),Host(),'operation'))
    assert calls==['keys','compute']
    assert '_cloud_init' not in config


def test_dag_never_surfaces_arbitrary_exception_contents():
    class Host:
        def prepare_keys(self):raise ValueError('secret sentinel')
    with pytest.raises(DeployError) as e:
        asyncio.run(cli.converge({},None,None,Host(),'operation'))
    assert 'sentinel' not in str(e.value)
