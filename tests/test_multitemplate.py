import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from videocut.detect import (combine_detections, detect_events, edge_features, get_profile, list_profiles, prepare_template,
                             profile_templates, save_profile, select_events, template_score, update_template)
from videocut.media import ROOT, ffmpeg_args, run
from videocut.pipeline import generate
from videocut.profile_test import test_profile
from videocut.review import review_exclusions, review_signature


def template(ident='cue', role='trigger', event_type='kill', roi=None, threshold=None):
    return {'id': ident, 'name': ident, 'role': role, 'event_type': event_type,
            'roi': roi or [0, 0, 60, 60], 'threshold': threshold}


def track(begin=8, end=16, total=48):
    return [0.] * begin + [.97] * (end-begin) + [0.] * (total-end)


class ClassificationTests(unittest.TestCase):
    def test_multiple_types_and_duplicate_background_samples(self):
        templates = [template(), template('bright'), template('head', event_type='headshot')]
        events = combine_detections(templates, [track(), track(9), track(24, 32)], 8, .82)
        self.assertEqual(len(events), 2)
        self.assertEqual([e['event_types'] for e in events], [['kill'], ['headshot']])
        self.assertEqual(len(events[0]['matches']), 2)
        self.assertEqual(events[0]['samples'], 8)
        self.assertEqual(events[0]['classification'], 'unknown')

    def test_player_npc_unknown_and_conflict(self):
        templates = [template(), template('player', 'player'), template('npc', 'npc')]
        for player, npc, expected in [(track(), track(0, 0), 'player'), (track(0, 0), track(), 'npc'),
                                      (track(), track(), 'unknown'), (track(0, 0), track(0, 0), 'unknown')]:
            with self.subTest(expected=expected):
                events = combine_detections(templates, [track(), player, npc], 8, .82)
                self.assertEqual(events[0]['classification'], expected)
                self.assertEqual(len(select_events(events)), 0 if expected == 'npc' else 1)

    def test_helper_alone_never_triggers_and_single_frame_never_filters(self):
        templates = [template(), template('npc', 'npc')]
        self.assertEqual(combine_detections(templates, [track(0, 0), track()], 8, .82), [])
        event = combine_detections(templates, [track(), track(8, 9)], 8, .82)[0]
        self.assertEqual(event['classification'], 'unknown')

    def test_distant_or_wrong_type_evidence_is_not_reused(self):
        for helper, scores in [(template('npc', 'npc'), track(20, 28)),
                               (template('npc', 'npc', 'headshot'), track())]:
            event = combine_detections([template(), helper], [track(), scores], 8, .82)[0]
            self.assertEqual(event['classification'], 'unknown')

    def test_separate_simultaneous_regions_are_not_merged_or_assigned_same_evidence(self):
        items = [template(), template('another', roi=[100, 100, 60, 60]), template('npc', 'npc', 'all')]
        events = combine_detections(items, [track(), track(), track()], 8, .82)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['classification'] == 'unknown' for e in events))

    def test_independent_threshold_overrides_default(self):
        events = combine_detections([template(threshold=.99), template('head', threshold=.9)], [track(), track()], 8, .98)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['matches'][0]['id'], 'head')

    def test_manual_exclusion_keeps_other_unknown_events(self):
        events = combine_detections([template()], [[max(a,b) for a,b in zip(track(),track(32,40))]], 8, .82)
        kept = select_events(events, [[1, 1.5]])
        self.assertEqual([e['time'] for e in kept], [4])
        self.assertEqual(events[0]['exclusion_reason'], '手动排除')


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='videocut-profile-')
        self.addCleanup(self.temp.cleanup)
        patcher = patch('videocut.detect.PROFILES', Path(self.temp.name))
        patcher.start(); self.addCleanup(patcher.stop)
        self.image = np.full((100, 160), 30, np.uint8)
        cv2.putText(self.image, 'K', (30, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 240, 3)
        self.frame = patch('videocut.detect.preview_frame', return_value=cv2.imencode('.png', self.image)[1].tobytes())
        self.frame.start(); self.addCleanup(self.frame.stop)
        self.rect = [.1, .2, .5, .6]

    def test_append_and_disable_preserve_old_samples_and_change_revision(self):
        first = save_profile('test', 1, self.rect, '游戏', template_name='普通击杀')
        second = save_profile('test', 1, self.rect, 'ignored', profile_id=first['id'], event_type='headshot')
        self.assertEqual(len(second['templates']), 2)
        self.assertEqual(second['name'], '游戏')
        self.assertEqual(first['templates'][0], second['templates'][0])
        self.assertNotEqual(first['revision'], second['revision'])
        updated = update_template(first['id'], first['templates'][0]['id'], False, .9)
        self.assertFalse(updated['templates'][0]['enabled'])
        self.assertEqual(len(next(p for p in list_profiles() if p['id'] == first['id'])['templates']), 2)
        with self.assertRaisesRegex(ValueError, '至少启用'):
            update_template(first['id'], second['templates'][1]['id'], False, None)
        self.assertTrue(get_profile(first['id'])['templates'][1]['enabled'])

    def test_legacy_upgrade_keeps_template_image(self):
        directory = Path(self.temp.name)/'old'; directory.mkdir()
        old = {'id': 'old', 'name': '旧配置', 'kind': 'template', 'reference_size': [160,100],
               'roi': [0,0,80,80], 'threshold': .82}
        (directory/'profile.json').write_text(json.dumps(old), encoding='utf-8')
        image = cv2.imencode('.png', self.image[:40,:40])[1].tobytes()
        (directory/'template.png').write_bytes(image)
        saved = save_profile('test', 1, self.rect, 'new', profile_id='old')
        self.assertEqual(saved['templates'][0]['image'], 'template.png')
        self.assertEqual((directory/'template.png').read_bytes(), image)
        self.assertEqual(profile_templates(old)[0]['match_mode'], 'gray')

    def test_erase_mask_and_invalid_empty_mask(self):
        _, mask, _, _ = prepare_template('test', 1, self.rect, erase=[[0,0,.2,1]])
        self.assertFalse(mask[:, :16].any())
        for options in [{'erase': [[0,0,1,1]]}, {'mask_mode': 'bad'}, {'cutoff': float('nan')}, {'erase': [[0,0,-1,1]]}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                prepare_template('test', 1, self.rect, **options)

    def test_new_helper_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '事件图标'):
            save_profile('test', 1, self.rect, 'test', role='npc')

    def test_masks_ignore_changed_background_and_empty_regions_are_finite(self):
        image = np.full((80,100), 35, np.uint8)
        cv2.rectangle(image, (30,25), (65,55), 240, 3)
        mask = np.zeros_like(image);mask[20:61,25:71] = 255
        other = image.copy();other[:15] = 210;other[65:] = 170;other[:,:20] = 120
        edges = edge_features(image)
        self.assertGreater(template_score(other, edges, mask, 'edges'), .99)
        self.assertLess(template_score(np.zeros_like(image), edges, mask, 'edges'), .5)
        self.assertLess(template_score(other, image), .82)


class ReviewTests(unittest.TestCase):
    def test_review_rejects_changed_source_profile_threshold_and_bad_spans(self):
        with tempfile.TemporaryDirectory(prefix='videocut-review-') as directory:
            video = Path(directory)/'video.mp4';video.write_bytes(b'fixture')
            profile = {'id':'test','revision':'one'}
            signature = review_signature(video, profile, .82)
            spec = {'review': {'signature':signature, 'excluded': [[1,2]]}}
            self.assertEqual(review_exclusions(spec,video,profile,.82), [[1,2]])
            for config, threshold in [({**profile, 'revision':'two'},.82), (profile,.9)]:
                with self.assertRaisesRegex(ValueError, '失效'):
                    review_exclusions(spec,video,config,threshold)
            for span in [[2,1], [float('nan'),2], [-1,2], [1], None]:
                with self.assertRaises(ValueError):
                    review_exclusions({'review': {'signature':signature,'excluded':[span]}},video,profile,.82)
            video.write_bytes(b'changed video')
            with self.assertRaisesRegex(ValueError, '失效'):
                review_exclusions(spec,video,profile,.82)


def make_multi_fixture(folder):
    """Different icons, identical kill icons, changing backgrounds, conflicting markers."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/'multiple-prompts.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 16, (960,540))
    if not writer.isOpened():
        raise RuntimeError('Cannot create multi-template fixture')
    try:
        for i in range(16*12):
            frame = np.full((540,960,3), 20+(i*3)%110, np.uint8)
            t = i/16
            for start, icon, marker in [(1,'X','P'), (3,'X','N'), (5,'H',''), (7,'X','PN'), (9,'X','')]:
                if start <= t < start+.75:
                    cv2.putText(frame,icon,(410,180),cv2.FONT_HERSHEY_SIMPLEX,1.2,(245,245,245),3)
                    if 'P' in marker:cv2.putText(frame,'P',(500,180),cv2.FONT_HERSHEY_SIMPLEX,1.2,(245,245,245),3)
                    if 'N' in marker:cv2.putText(frame,'N',(580,180),cv2.FONT_HERSHEY_SIMPLEX,1.2,(245,245,245),3)
            # Helper without a trigger must not produce an event.
            if 10.5 <= t < 11.25:cv2.putText(frame,'N',(580,180),cv2.FONT_HERSHEY_SIMPLEX,1.2,(245,245,245),3)
            writer.write(frame)
    finally:
        writer.release()
    return path


def fixture_profile(video):
    rect = [400/960,140/540,50/960,50/540]
    p = save_profile(video,1.2,rect,'合成多模板验证',template_name='普通击杀',sample_threshold=.8)
    p = save_profile(video,9.2,rect,'',profile_id=p['id'],template_name='普通击杀 · 另一背景',sample_threshold=.8)
    p = save_profile(video,5.2,rect,'',profile_id=p['id'],event_type='headshot',template_name='爆头',sample_threshold=.8)
    p = save_profile(video,1.2,[490/960,140/540,50/960,50/540],'',profile_id=p['id'],role='player',event_type='all',template_name='玩家标记',sample_threshold=.85)
    return save_profile(video,3.2,[570/960,140/540,50/960,50/540],'',profile_id=p['id'],role='npc',event_type='all',template_name='小兵标记',sample_threshold=.85)


@unittest.skipUnless(os.environ.get('VIDEOCUT_INTEGRATION') == '1', 'Requires real FFmpeg')
class MultiTemplateIntegrationTests(unittest.TestCase):
    def test_real_scan_classification_review_and_render(self):
        video = make_multi_fixture(ROOT/'work'/'multi-template-fixture')
        with tempfile.TemporaryDirectory(prefix='videocut-multi-') as directory, patch('videocut.detect.PROFILES', Path(directory)):
            profile = fixture_profile(video)
            events = detect_events(video,profile)
            self.assertEqual(len(events),5, events)
            self.assertEqual([e['classification'] for e in events], ['player','npc','unknown','unknown','unknown'])
            self.assertEqual(len(events[0]['matches']),2)
            self.assertEqual(events[2]['event_types'],['headshot'])
            spec = {'video':str(video),'profile':profile['id'],'target':30,'strength':0}
            result = test_profile({**spec,'start':.3,'duration':11})
            self.assertEqual(len(result['events']),5)
            excluded = result['events'][-1]
            montage = generate({**spec,'review':{'signature':result['review_signature'],'excluded':[[excluded['time'],excluded['end']]]}})
            self.assertEqual(montage['candidate_count'],5)
            self.assertEqual(montage['excluded_count'],2)
            self.assertEqual([e['classification'] for e in montage['events']],['player','unknown','unknown'])
            saved = json.loads((Path(montage['output']).parent/'events.json').read_text(encoding='utf-8'))
            self.assertEqual([e['included'] for e in saved],[True,False,True,True,False])
            run(ffmpeg_args()+['-i',montage['output'],'-f','null','-'],timeout=60)


if __name__ == '__main__':
    unittest.main()
