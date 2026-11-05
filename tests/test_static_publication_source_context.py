"""The actual trusted CLI supplies the canonical SDK's closed blueprint reader."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('static_context_publication',ROOT/'godot-static-delivery/publish.py')
publication=importlib.util.module_from_spec(spec);spec.loader.exec_module(publication)
SOURCE='a'*40


class BlueprintReached(RuntimeError):
    pass


class StaticPublicationSourceContext(unittest.TestCase):
    def setUp(self):
        self.sdk=Path(os.environ['TOOLS_ROOT']).resolve()
        self.assertTrue((self.sdk/'cookbook/godot_static.py').is_file(),'prepare the accepted cookbook SDK')
        sys.path.insert(0,str(self.sdk/'cookbook'))
        import godot_static
        self.static=godot_static
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        values={'deploy/config.yml':'compose: compose.yml\n',
                'deploy/compose.yml':'services:\n  game-gate:\n    image: ghcr.io/example/game-gate:${APP_VERSION:-latest}\n'}
        for name,text in values.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        declaration={'mode':'godot-static','komizo_ref':'b'*40,'project':'game','static':{
            'config':'deploy/config.yml','compose':'deploy/compose.yml','release_manifest':'public/release-manifest.json',
            'record':False,'recovery':False,'production':False}}
        (self.root/'deploy/delivery.json').write_text(json.dumps(declaration))
        (self.root/'deploy/release.json').write_text(json.dumps({'project':'game','image_base':'ghcr.io/example/game'}))
        self.config={'recipe':'godot-game','delivery':{'definition':'deploy/delivery.json'},'artifacts':{
            'definition':'deploy/release.json','release_metadata':[godot_static.CONFIG_ARTIFACT,godot_static.MANIFEST_ARTIFACT]}}
        self.blueprint={'version':1,'repository':'example/game','source_commit':SOURCE,
            'config':{'path':'deploy/config.yml','text':values['deploy/config.yml'],
                      'sha256':hashlib.sha256(values['deploy/config.yml'].encode()).hexdigest()},
            'compose':{'path':'deploy/compose.yml','text':values['deploy/compose.yml'],
                       'sha256':hashlib.sha256(values['deploy/compose.yml'].encode()).hexdigest()},
            'hostnames':'game.example.test -> game-gate\n','production_stack':{
                'services':{'game-gate':{'image':'ghcr.io/example/game-gate@'+godot_static.PLACEHOLDER,
                    'user':'1000:1000','read_only':True,'cap_drop':['ALL'],
                    'security_opt':['no-new-privileges:true']}}}}
        target=self.root/godot_static.CONFIG_ARTIFACT;target.parent.mkdir(parents=True);target.write_text(json.dumps(self.blueprint))
        self.environment={'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'push','GITHUB_SHA':SOURCE}

    def test_actual_cli_supplies_missing_context_to_actual_canonical_blueprint_reader(self):
        # Counterfactual reproduces the observed adapter failure with a valid blueprint.
        with self.assertRaises(KeyError):self.static.verify_blueprint(self.root,self.config,self.environment)
        observed=[]
        def admitted(root,tools,source,tools_ref,incoming,environment):
            result=self.static.verify_blueprint(self.root,self.config,environment)
            self.assertEqual(result,self.blueprint)
            observed.append(environment['SOURCE_COMMIT'])
            raise BlueprintReached('canonical blueprint verified before publication')
        argv=['publish.py','--tools',str(self.sdk),'--tools-ref','c'*40,'--source',SOURCE]
        with patch.dict(os.environ,self.environment,clear=True),patch.object(sys,'argv',argv),\
                patch.object(publication,'admit',side_effect=admitted),\
                patch.object(publication,'publish_gate',side_effect=AssertionError('registry effect')),\
                self.assertRaises(BlueprintReached):publication.main()
        self.assertEqual(observed,[SOURCE])

    def test_conflicting_source_branch_or_event_refuses_before_admission_or_effects(self):
        argv=['publish.py','--tools',str(self.sdk),'--tools-ref','c'*40,'--source',SOURCE]
        for changed in [{'SOURCE_COMMIT':'d'*40},{'SOURCE_COMMIT':''},{'GITHUB_SHA':'d'*40},
                {'GITHUB_REF':'refs/pull/1/merge'},{'GITHUB_EVENT_NAME':'pull_request'}]:
            environment=self.environment|changed
            with self.subTest(changed=changed),patch.dict(os.environ,environment,clear=True),\
                    patch.object(sys,'argv',argv),patch.object(publication,'admit') as admit,\
                    self.assertRaises(SystemExit):publication.main()
            admit.assert_not_called()

    def test_bound_context_does_not_admit_stale_or_changed_native_blueprints(self):
        environment=publication.source_environment(SOURCE,self.environment)
        target=self.root/self.static.CONFIG_ARTIFACT
        stale=copy.deepcopy(self.blueprint);stale['source_commit']='d'*40;target.write_text(json.dumps(stale))
        with self.assertRaisesRegex(ValueError,'another source'):self.static.verify_blueprint(self.root,self.config,environment)
        target.write_text(json.dumps(self.blueprint))
        (self.root/'deploy/compose.yml').write_text(self.blueprint['compose']['text']+'# changed\n')
        with self.assertRaisesRegex(ValueError,'reviewed native source'):self.static.verify_blueprint(self.root,self.config,environment)


if __name__=='__main__':unittest.main()
