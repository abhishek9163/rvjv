import io
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from portal.models import Message, User


class ChatPresenceTests(TestCase):
    def setUp(self):
        self.me = User.objects.create_user(username='me', password='pass123', system_role='MANAGER')
        self.buddy = User.objects.create_user(username='buddy', password='pass123', system_role='DEO')
        self.client.force_login(self.me)

    def test_heartbeat_marks_user_online(self):
        self.assertIsNone(self.me.last_seen)
        response = self.client.post(reverse('api_chat_heartbeat'))
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertIsNotNone(self.me.last_seen)
        self.assertTrue(self.me.is_online())

    def test_presence_lists_online_users_only(self):
        self.buddy.last_seen = timezone.now()
        self.buddy.save()
        stale = User.objects.create_user(username='stale', password='pass123', system_role='DEO')
        stale.last_seen = timezone.now() - timedelta(minutes=10)
        stale.save()

        data = self.client.get(reverse('api_chat_presence')).json()
        self.assertIn(str(self.buddy.id), data['online'])
        self.assertNotIn(str(stale.id), data['online'])

    def test_chat_room_renders_online_dot(self):
        self.buddy.last_seen = timezone.now()
        self.buddy.save()
        response = self.client.get(reverse('chat_room'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'presence-dot on')


class ChatAudioMessageTests(TestCase):
    def setUp(self):
        self.me = User.objects.create_user(username='alice', password='pass123', system_role='MANAGER')
        self.buddy = User.objects.create_user(username='bob', password='pass123', system_role='DEO')
        self.client.force_login(self.me)

    def test_send_audio_creates_message_with_file(self):
        blob = SimpleUploadedFile('voice-note.webm', io.BytesIO(b'RIFFfake-audio-bytes').getvalue(),
                                  content_type='audio/webm')
        response = self.client.post(reverse('api_send_audio'), {
            'receiver_id': self.buddy.id, 'audio': blob,
        })
        self.assertEqual(response.json()['status'], 'success')
        msg = Message.objects.get()
        self.assertEqual(msg.file_type, 'audio')
        self.assertTrue(msg.file)

    def test_get_messages_returns_audio_payload(self):
        msg = Message.objects.create(sender=self.me, receiver=self.buddy, content='', file_type='audio')
        msg.file.save('voice.webm', io.BytesIO(b'audio'), save=True)
        data = self.client.get(reverse('api_get_messages', args=[self.buddy.id])).json()
        self.assertEqual(data['messages'][0]['file_type'], 'audio')
        self.assertTrue(data['messages'][0]['file_url'])

    def test_non_audio_upload_is_rejected(self):
        blob = SimpleUploadedFile('evil.exe', b'MZ...', content_type='application/octet-stream')
        response = self.client.post(reverse('api_send_audio'), {
            'receiver_id': self.buddy.id, 'audio': blob,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)


class DashboardPreviewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='boss', password='pass123', system_role='MANAGER')

    def test_dashboard_renders_card_previews_for_manager(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'b-card-preview-row')

    def test_deo_dashboard_renders_module_gated_previews(self):
        deo = User.objects.create_user(username='deo2', password='pass123', system_role='DEO',
                                       assigned_modules=['vehicle_movement', 'tyre_section'])
        self.client.force_login(deo)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Total entries', html)          # movements preview present
        self.assertNotIn('Entries this month', html)  # lubricants module not assigned

