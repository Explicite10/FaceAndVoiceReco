from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

CheckStatus = Literal["passed", "failed", "not_evaluated"]


class Check(BaseModel):
    status: CheckStatus
    code: Optional[str] = Field(None, description="Codigo estable, solo cuando status es 'failed'.")
    message: str


class Result(BaseModel):
    decision: Literal["approved", "rejected"]
    approved: bool
    failed_checks: List[str] = Field(description="Ej. 'face.single_face', 'liveness.anti_spoofing'.")
    summary: str


class Checks(BaseModel):
    face: Dict[str, Check]
    voice: Dict[str, Check]
    liveness: Dict[str, Check]
    challenge: Dict[str, Check] = Field(description="Gestos del reto: head_turn, blink, hand_raise y gesture_order.")


# ----- detalle
class Similarity(BaseModel):
    average_percent: float
    min_score: Optional[float] = None
    threshold_score: float = Field(description="Similitud coseno equivalente a threshold_percent.")
    threshold_percent: float = Field(description="Porcentaje minimo usado para decidir el match.")


class FaceSegment(BaseModel):
    index: int
    time_sec: Optional[float] = None
    face_detected: bool
    similarity_score: Optional[float] = None
    similarity_percent: Optional[float] = None
    match: bool


class FacesInVideo(BaseModel):
    max_per_frame: int
    frames_with_multiple: int
    first_multiple_sec: Optional[float] = None
    min_frames_to_reject: int


class FaceDetails(BaseModel):
    similarity: Similarity
    segments: List[FaceSegment]
    faces_in_video: FacesInVideo


class PhraseMatch(BaseModel):
    match_percent: float
    threshold_percent: float
    word_error_rate: Optional[float] = None
    missing_words: List[str]
    extra_words: List[str]
    ignored_words: List[str] = []


class AudioInfo(BaseModel):
    duration_sec: float
    speech_sec: float
    speech_ratio: float
    phrase_window: Optional[dict] = None


class Transcription(BaseModel):
    language: Optional[str] = None
    language_probability: Optional[float] = None
    word_confidence_probability: Optional[float] = None
    no_speech_probability: Optional[float] = None
    avg_log_probability: Optional[float] = None


class VoiceDetails(BaseModel):
    expected_phrase: str
    transcript: str
    phrase_match: PhraseMatch
    audio: AudioInfo
    transcription: Optional[Transcription] = None


class AntiSpoofing(BaseModel):
    real_probability: float
    threshold_probability: float
    by_segment_probability: List[Optional[float]]


class LipSync(BaseModel):
    face_tracked_ratio: float
    mouth_motion_speaking: Optional[float] = None
    mouth_motion_silent: Optional[float] = None
    audio_correlation: Optional[float] = None
    audio_lag_ms: Optional[int] = None


class LivenessDetails(BaseModel):
    anti_spoofing: AntiSpoofing
    lip_sync: LipSync


GestureName = Literal["turn_left", "turn_right", "blink", "raise_left_hand", "raise_right_hand"]


class GestureResult(BaseModel):
    gesture: GestureName
    detected: bool
    time_sec: Optional[float] = Field(None, description="Primera vez que se detecto el gesto.")


class GestureMeasurements(BaseModel):
    frames_analyzed: int
    max_turn_left: Optional[float] = Field(None, description="Giro maximo: desplazamiento de la nariz / ancho de la cara.")
    max_turn_right: Optional[float] = None
    max_blink_probability: Optional[float] = None
    turn_threshold: float
    blink_threshold_probability: float


class ChallengeDetails(BaseModel):
    challenge_id: str
    phrase: str
    gestures: List[GestureResult]
    in_order: bool
    measurements: Optional[GestureMeasurements] = None


class Details(BaseModel):
    face: Optional[FaceDetails] = None
    voice: Optional[VoiceDetails] = None
    liveness: Optional[LivenessDetails] = None
    challenge: Optional[ChallengeDetails] = None


class ErrorItem(BaseModel):
    component: Literal["face", "voice", "liveness", "challenge"]
    code: Optional[str] = None
    message: str
    http_status: int


class Meta(BaseModel):
    api_version: str
    models: Dict[str, str]


class ChallengeStep(BaseModel):
    gesture: GestureName
    instruction: str = Field(description="Texto para mostrar al usuario. Izquierda/derecha son las del usuario.")


class ChallengeResponse(BaseModel):
    challenge_id: str = Field(description="Se manda en /api/verify-identity. Sirve una sola vez.")
    phrase: str = Field(description="Frase que el usuario lee en voz alta despues de los gestos.")
    gestures: List[ChallengeStep] = Field(description="Gestos a hacer, en este orden, antes de leer la frase.")
    expires_in_sec: int


class VerifyIdentityResponse(BaseModel):
    request_id: str
    timestamp: str = Field(description="ISO 8601 en UTC.")
    processing_time_ms: int
    result: Result
    checks: Checks
    details: Details
    errors: List[ErrorItem]
    meta: Meta


# ----- foto contra identificacion
class PhotoChecks(BaseModel):
    face: Dict[str, Check]
    quality: Dict[str, Check]
    liveness: Dict[str, Check]


class PhotoSimilarity(BaseModel):
    score: float = Field(description="Similitud coseno.")
    percent: float = Field(description="Mapeo sigmoide para mostrar. No es probabilidad.")
    threshold_score: float = Field(description="Similitud coseno equivalente a threshold_percent.")
    threshold_percent: float = Field(description="Porcentaje minimo usado para decidir el match.")


class FaceQuality(BaseModel):
    size_px: int = Field(description="Lado menor de la cara.")
    sharpness: float = Field(description="Varianza del Laplaciano de la cara a 112 px.")
    brightness: float = Field(description="Brillo medio de la cara, 0 a 255.")
    detector_score: float


class PhotoAntiSpoofing(BaseModel):
    real_probability: float
    threshold_probability: float


class PhotoDetails(BaseModel):
    similarity: Optional[PhotoSimilarity] = None
    faces_in_photo: int
    id_face_quality: Optional[FaceQuality] = Field(None, description="Informativo: no se valida.")
    photo_face_quality: Optional[FaceQuality] = None
    anti_spoofing: Optional[PhotoAntiSpoofing] = None


class VerifyPhotoResponse(BaseModel):
    request_id: str
    timestamp: str = Field(description="ISO 8601 en UTC.")
    processing_time_ms: int
    result: Result
    checks: PhotoChecks
    details: PhotoDetails
    meta: Meta
