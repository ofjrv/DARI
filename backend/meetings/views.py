# 회의실 생성, 대기실 조회, 토큰 발급 API
import threading
import urllib.parse
import uuid
from django.db import models, transaction
from django.conf import settings
from rest_framework import status, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone
from cards.models import Card
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import MeetingSession, MeetingParticipant, SpeechCard, MeetingChatMessage, MeetingSummary, MeetingMemo, ActionItem
from .utils import generate_media_server_token
from .services import MeetingSummaryPipeline, MeetingShareFormatter
from . import tracker_client
from .serializers import (
    MeetingSessionSerializer,
    ParticipantSerializer,
    SpeechCardSerializer,
    MeetingChatMessageSerializer,
    MeetingSummaryTabSerializer,
    MeetingMemoSerializer,
    ActionItemSerializer
)

User = get_user_model()
def resolve_meeting(room_code):
    """
    실제 room_code 또는 프론트 목데이터 room_code를
    실제 MeetingSession으로 변환한다.
    """
    if room_code.startswith("00000000") or room_code.startswith("demo-"):
        return MeetingSession.objects.order_by('-created_at').first()

    return MeetingSession.objects.filter(room_code=room_code).first()

class CreateMeetingView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        title = request.data.get('title', '신규 회의')
        scheduled_start_time = request.data.get('scheduled_start_time')

        room_code = request.data.get('room_code')

        while MeetingSession.objects.filter(room_code=room_code).exists():
            room_code = str(uuid.uuid4())[:8]

        participant_usernames = request.data.get('participants', [])

        if participant_usernames is None:
            participant_usernames = []

        if not isinstance(participant_usernames, list):
            return Response(
                {'error': 'participants는 사용자 username 목록이어야 합니다.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        participant_usernames = list(dict.fromkeys(
            str(username).strip()
            for username in participant_usernames
            if str(username).strip()
        ))

        invited_users = []

        for username in participant_usernames:
            invited_user = User.objects.filter(username=username).first()

            if not invited_user:
                return Response(
                    {
                        'error': f'존재하지 않는 사용자입니다: {username}',
                        'username': username
                    },
                    status=status.HTTP_404_NOT_FOUND
                )

            if invited_user == request.user:
                return Response(
                    {'error': '회의 호스트 본인은 참가자로 초대할 수 없습니다.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            invited_users.append(invited_user)

        try:
            with transaction.atomic():
                meeting = MeetingSession.objects.create(
                    room_code=room_code,
                    title=title,
                    host=request.user,
                    scheduled_start_time=scheduled_start_time,
                )

                participants = []

                for invited_user in invited_users:
                    participant = MeetingParticipant.objects.create(
                        meeting=meeting,
                        user=invited_user,
                        is_host=False,
                        status='PENDING',
                        is_active=False,
                    )
                    participants.append(participant)

            return Response(
                {
                    **MeetingSessionSerializer(meeting).data,
                    'invited_participants': ParticipantSerializer(
                        participants,
                        many=True
                    ).data,
                },
                status=status.HTTP_201_CREATED
            )

        except Exception as e:
            print(f"🔥 회의 생성 중 에러 발생: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class PrejoinView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        meeting = resolve_meeting(room_code)

        if not meeting:
            return Response({'error': '생성된 회의가 없습니다. 먼저 회의를 만들어주세요.'}, status=status.HTTP_404_NOT_FOUND)

        if meeting.status == 'ENDED':
            return Response({'error': '이미 종료된 회의입니다.'}, status=status.HTTP_400_BAD_REQUEST)

        active_participants = meeting.participants.filter(is_active=True)

        response_data = {
            'room_code': meeting.room_code,
            'title': meeting.title,
            'host_id': str(meeting.host_id),
            'status': meeting.status,
            'participants_count': active_participants.count(),
            'participants': ParticipantSerializer(active_participants, many=True).data
        }

        if settings.DARI_DEMO_MODE and meeting.room_code.startswith('demo-'):
            response_data['chat_history'] = [
                {
                    'id': str(message.id),
                    'sender_id': str(message.sender_id),
                    'sender_name': message.sender.username,
                    'message': message.message,
                    'is_speech_card': message.is_speech_card,
                }
                for message in meeting.chat_messages.select_related('sender').all()
            ]
            response_data['transcript_history'] = [
                {
                    'id': str(transcript.id),
                    'speaker_id': str(transcript.speaker_id),
                    'speaker_name': transcript.speaker.username,
                    'original_text': transcript.original_text,
                    'translations': transcript.translations,
                }
                for transcript in meeting.transcripts.select_related('speaker').all()
            ]

        return Response(response_data)

class MediaTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        return self._issue_token(request, room_code)

    def post(self, request, room_code):
        return self._issue_token(request, room_code)

    def _issue_token(self, request, room_code):
        meeting = resolve_meeting(room_code)

        if not meeting:
            return Response(
                {'error': '회의를 찾을 수 없습니다.'},
                status=status.HTTP_404_NOT_FOUND
            )

        token = generate_media_server_token(
            room_code=meeting.room_code,
            user_id=request.user.id,
            username=request.user.username
        )

        return Response({'token': token})


class SpeechCardListView(APIView):
    """발언카드 조회 API: 로그인 사용자가 사전에 생성한 발언카드 목록 반환"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cards = Card.objects.filter(owner=request.user)
        return Response([
            {
                'id': str(card.id),
                'persona_name': card.partner_tag,
                'situation': card.situation_label,
                'korean_script': card.text_ko,
                'translated_script': card.text_translated,
                'target_lang': card.language_code.upper(),
                'created_at': card.created_at,
            }
            for card in cards
        ], status=status.HTTP_200_OK)


class ParticipantManageView(APIView):
    """참가자 관리 API: 목록 조회 및 사용자 초대"""
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        meeting = get_object_or_404(
            MeetingSession,
            room_code=room_code
        )

        participants = meeting.participants.filter(is_active=True)

        return Response(
            ParticipantSerializer(participants, many=True).data
        )

    def post(self, request, room_code):
        """사용자 초대 API"""

        meeting = get_object_or_404(
            MeetingSession,
            room_code=room_code
        )

        if meeting.host != request.user:
            return Response(
                {'error': '참여자를 초대할 권한이 없습니다.'},
                status=status.HTTP_403_FORBIDDEN
            )

        username = request.data.get('username')

        if not username:
            return Response(
                {'error': '초대할 사용자의 username을 입력해주세요.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        invited_user = User.objects.filter(
            username=username
        ).first()

        if not invited_user:
            return Response(
                {'error': '존재하지 않는 사용자입니다.'},
                status=status.HTTP_404_NOT_FOUND
            )

        if invited_user == request.user:
            return Response(
                {'error': '회의 호스트 본인은 초대할 수 없습니다.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        participant, created = MeetingParticipant.objects.get_or_create(
            meeting=meeting,
            user=invited_user,
            defaults={
                'is_host': False,
                'status': 'PENDING',
                'is_active': False,
            }
        )

        if not created:
            participant.status = 'PENDING'
            participant.is_active = False

            participant.save(
                update_fields=['status', 'is_active']
            )

        return Response(
            {
                'message': f'{username} 님을 회의에 초대했습니다.',
                'participant': ParticipantSerializer(participant).data,
            },
            status=status.HTTP_200_OK
        )

class KickParticipantView(APIView):
    """호스트 권한 참가자 내보내기 API"""
    permission_classes = [IsAuthenticated]

    def post(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)

        # 호스트 권한 체크
        if meeting.host != request.user:
            return Response({'error': '참가자를 내보낼 권한이 없습니다.'}, status=status.HTTP_403_FORBIDDEN)

        target_user_id = request.data.get('user_id')
        participant = MeetingParticipant.objects.filter(meeting=meeting, user_id=target_user_id).first()

        if participant:
            participant.is_active = False
            participant.save()

            # 실시간으로 연결되어 있는 대상 참가자의 WebSocket에 강퇴 신호를 보냄
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f'meeting_{room_code}',
                    {
                        'type': 'kicked',
                        'user_id': target_user_id,
                    }
                )

            return Response({'message': '참가자를 회의에서 내보냈습니다.'}, status=status.HTTP_200_OK)

        return Response({'error': '해당 참가자를 찾을 수 없습니다.'}, status=status.HTTP_404_NOT_FOUND)

class EndMeetingView(APIView):
    """
    [회의 종료 API]

    호스트 권한으로 회의를 종료하고,
    회의 참가자별 참여 기록을 tracker로 전달한 뒤
    백그라운드에서 AI 요약 & Action Item 파이프라인을 실행한다.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, room_code):
        meeting = get_object_or_404(
            MeetingSession,
            room_code=room_code
        )

        if meeting.host != request.user:
            return Response(
                {'error': '회의를 종료할 권한이 없습니다.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if meeting.status == 'ENDED':
            return Response(
                {
                    'message': '이미 종료된 회의입니다.',
                    'room_code': meeting.room_code,
                    'status': meeting.status
                },
                status=status.HTTP_200_OK
            )

        # --------------------------------------------------
        # 1. 회의 종료 시각 기록
        # --------------------------------------------------
        meeting.ended_at = timezone.now()
        meeting.status = 'ENDED'

        meeting.save(
            update_fields=[
                'status',
                'ended_at',
            ]
        )

        # --------------------------------------------------
        # 2. 아직 회의에 접속 중인 참가자는
        #    회의 종료 시각을 퇴장 시각으로 기록
        # --------------------------------------------------
        participants = (
            meeting.participants
            .select_related('user')
            .all()
        )

        for participant in participants:
            if participant.is_active and participant.left_at is None:
                participant.left_at = meeting.ended_at
                participant.is_active = False

                participant.save(
                    update_fields=[
                        'left_at',
                        'is_active',
                    ]
                )

        # --------------------------------------------------
        # 3. tracker로 전달할 참가자 데이터 생성
        # --------------------------------------------------
        tracker_participants = []

        for participant in participants:
            speaking_duration_seconds = 0

            if participant.joined_at and participant.left_at:
                speaking_duration_seconds = max(
                    0,
                    int(
                        (
                            participant.left_at
                            - participant.joined_at
                        ).total_seconds()
                    )
                )

            tracker_participants.append(
                {
                    'user_id': str(participant.user_id),
                    'local_timezone': (
                        participant.local_time_zone or 'UTC'
                    ),
                    'local_region': '',
                    'speaking_duration_seconds': (
                        speaking_duration_seconds
                    ),
                }
            )

        # --------------------------------------------------
        # 4. tracker에 참여 기록 전송
        # --------------------------------------------------
        try:
            tracker_result = tracker_client.ingest_participation(
                request,
                external_meeting_id=meeting.room_code,
                meeting_title=meeting.title,
                meeting_time_utc=meeting.ended_at,
                participants=tracker_participants,
            )

        except tracker_client.TrackerUnavailable as exc:
            print(f"🔥 tracker 참여 기록 전송 실패: {exc}")

            tracker_result = {
                'error': str(exc)
            }

        # --------------------------------------------------
        # 5. AI 회의 요약 및 Action Item 생성
        # --------------------------------------------------
        if settings.DARI_DEMO_MODE:
            MeetingSummaryPipeline.generate_summary_and_action_items(
                meeting.id
            )
        else:
            threading.Thread(
                target=MeetingSummaryPipeline.generate_summary_and_action_items,
                args=(meeting.id,)
            ).start()

        return Response(
            {
                'message': (
                    '회의가 성공적으로 종료되었으며, '
                    'AI 요약 생성이 시작되었습니다.'
                ),
                'room_code': meeting.room_code,
                'status': meeting.status,
                'tracker': tracker_result,
            },
            status=status.HTTP_200_OK
        )

class UserMeetingListView(APIView):
    """
    [상단 탭 UI용 회의 목록 조회 API]
    사용자가 호스트이거나 참가했던 회의 목록 반환
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        meetings = MeetingSession.objects.filter(
            models.Q(host=user) | models.Q(participants__user=user)
        ).filter(status='ENDED').distinct().order_by('-created_at')

        serializer = MeetingSummaryTabSerializer(meetings, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class MeetingReportDetailView(APIView):
    """
    [특정 회의 상세 리포트 조회 API]
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)

        summary_obj = getattr(meeting, 'summary', None)
        summary_content = summary_obj.content if summary_obj else "아직 생성된 회의 요약이 없습니다."

        memos = MeetingMemo.objects.filter(meeting=meeting, user=request.user)
        memo_serializer = MeetingMemoSerializer(memos, many=True)

        action_items = ActionItem.objects.filter(meeting=meeting)
        action_item_serializer = ActionItemSerializer(action_items, many=True)

        participants_data = []
        participants_data.append({
            'name': request.user.username if meeting.host == request.user else meeting.host.username,
            'is_host': True,
            'is_me': meeting.host == request.user
        })
        for p in meeting.participants.exclude(user=meeting.host):
            participants_data.append({
                'name': p.user.username,
                'is_host': False,
                'is_me': p.user == request.user
            })

        return Response({
            'room_code': meeting.room_code,
            'title': meeting.title,
            'display_header': f"{meeting.title} · {meeting.created_at.month}/{meeting.created_at.day}",
            'ai_summary': summary_content,
            'memos': memo_serializer.data,
            'action_items': action_item_serializer.data,
            'participants': participants_data
        }, status=status.HTTP_200_OK)


class MeetingMemoListCreateView(APIView):
    """
    [메모 목록 조회 및 작성 API]
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)
        memos = MeetingMemo.objects.filter(meeting=meeting, user=request.user)
        serializer = MeetingMemoSerializer(memos, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)
        content = request.data.get('content', '').strip()

        if not content:
            return Response({'error': '메모 내용을 입력해 주세요.'}, status=status.HTTP_400_BAD_REQUEST)

        memo = MeetingMemo.objects.create(
            meeting=meeting,
            user=request.user,
            content=content
        )
        serializer = MeetingMemoSerializer(memo)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MeetingMemoDeleteView(APIView):
    """
    [메모 삭제 API]
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, memo_id):
        memo = get_object_or_404(MeetingMemo, id=memo_id, user=request.user)
        memo.delete()
        return Response({'message': '메모가 삭제되었습니다.'}, status=status.HTTP_200_OK)


class ActionItemUpdateView(APIView):
    """
    [Action Item 수정 API (PATCH)]
    담당자 지정 / 마감일 지정 / 완료 여부 변경
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, item_id):
        action_item = get_object_or_404(ActionItem, id=item_id)

        # 1. 완료 여부
        if 'is_completed' in request.data:
            value = request.data.get('is_completed')

            # 문자열로 들어오는 경우도 안전하게 처리
            if isinstance(value, bool):
                action_item.is_completed = value
            elif str(value).lower() == 'true':
                action_item.is_completed = True
            elif str(value).lower() == 'false':
                action_item.is_completed = False
            else:
                return Response(
                    {'error': 'is_completed는 true 또는 false여야 합니다.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # 2. 담당자
        if 'assignee' in request.data:
            assignee = request.data.get('assignee')

            if assignee is None:
                action_item.assignee = '미지정'
            else:
                assignee = str(assignee).strip()
                action_item.assignee = assignee or '미지정'

        # 3. 마감일
        if 'due_date' in request.data:
            due_date = request.data.get('due_date')

            if due_date in [None, '']:
                action_item.due_date = None
            else:
                try:
                    from datetime import datetime

                    action_item.due_date = datetime.strptime(
                        str(due_date),
                        '%Y-%m-%d'
                    ).date()

                except ValueError:
                    return Response(
                        {
                            'error': 'due_date 형식이 올바르지 않습니다.',
                            'expected_format': 'YYYY-MM-DD'
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )

        action_item.save()

        serializer = ActionItemSerializer(action_item)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK
        )

class MeetingShareTextView(APIView):
    """
    [Slack / 클립보드 복사 및 mailto 생성 API]
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)

        formatted_text = MeetingShareFormatter.generate_formatted_text(meeting, request.user)

        email_subject = f"[DARI] {meeting.title} 회의 요약 및 Action Items"
        encoded_subject = urllib.parse.quote(email_subject)
        encoded_body = urllib.parse.quote(formatted_text)
        mailto_link = f"mailto:?subject={encoded_subject}&body={encoded_body}"

        return Response({
            'meeting_title': meeting.title,
            'formatted_text': formatted_text,
            'mailto_link': mailto_link
        }, status=status.HTTP_200_OK)


class MeetingEmailSendView(APIView):
    """
    [Django SMTP 기반 회의 결과 이메일 직접 전송 API]
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, room_code):
        meeting = get_object_or_404(MeetingSession, room_code=room_code)
        target_emails = request.data.get('emails', [])

        if not target_emails:
            participant_emails = [
                p.user.email for p in meeting.participants.all() if p.user.email
            ]
            if meeting.host.email:
                participant_emails.append(meeting.host.email)
            target_emails = list(set(participant_emails))

        if not target_emails:
            return Response(
                {'error': '결과를 전송할 이메일 주소가 없습니다.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        formatted_text = MeetingShareFormatter.generate_formatted_text(meeting, request.user)
        subject = f"[DARI] {meeting.title} 회의 요약 및 Action Items"

        try:
            send_mail(
                subject=subject,
                message=formatted_text,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dari.com'),
                recipient_list=target_emails,
                fail_silently=False,
            )
            return Response({
                'message': f'{len(target_emails)}명에게 회의 결과가 성공적으로 전송되었습니다.',
                'sent_to': target_emails
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({
                'error': '이메일 전송 중 오류가 발생했습니다. 네트워크 상태를 확인하고 잠시 후 다시 시도해 주세요.',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class HomeMeetingListView(APIView):
    """
    [홈 화면 API]

    사용자가 호스트인 회의 또는
    초대를 수락한 회의만 예정된 회의 목록에 반환
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        meetings = MeetingSession.objects.filter(
            models.Q(host=request.user) |
            models.Q(
                participants__user=request.user,
                participants__status='ACCEPTED'
            ),
            status='WAITING'
        ).distinct().order_by('-created_at')

        serializer = MeetingSessionSerializer(
            meetings,
            many=True
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK
        )

class InvitationListView(APIView):
    """
    [받은 회의 초대 목록 조회 API]
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        invitations = MeetingParticipant.objects.filter(user=request.user, status='PENDING')
        data = []
        for inv in invitations:
            data.append({
                "meeting_id": inv.meeting.id,
                "room_code": inv.meeting.room_code,
                "title": inv.meeting.title,
                "host_name": inv.meeting.host.username if inv.meeting.host else "호스트",
                "created_at": inv.meeting.created_at,
            })
        return Response(data, status=status.HTTP_200_OK)


class RespondInvitationView(APIView):
    """
    [회의 초대 수락 / 거절 처리 API]
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_id):
        action = request.data.get('action')

        participant = get_object_or_404(
            MeetingParticipant,
            meeting_id=meeting_id,
            user=request.user
        )
        if participant.status != 'PENDING':
            return Response(
                {
                    'error': '이미 처리된 초대입니다.',
                    'status': participant.status
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        if action == 'accept':
            participant.status = 'ACCEPTED'
            participant.is_active = True

            participant.save(
                update_fields=['status', 'is_active']
            )

            return Response(
                {
                    'message': '회의 참가가 수락되었습니다.',
                    'status': 'ACCEPTED',
                    'meeting_id': str(participant.meeting.id),
                    'room_code': participant.meeting.room_code,
                },
                status=status.HTTP_200_OK
            )

        elif action == 'reject':
            participant.status = 'REJECTED'
            participant.is_active = False

            participant.save(
                update_fields=['status', 'is_active']
            )

            return Response(
                {
                    'message': '회의 초대를 거절했습니다.',
                    'status': 'REJECTED',
                },
                status=status.HTTP_200_OK
            )

        else:
            return Response(
                {
                    'error': '올바르지 않은 요청입니다. '
                             '(action: accept/reject 필요)'
                },
                status=status.HTTP_400_BAD_REQUEST
            )