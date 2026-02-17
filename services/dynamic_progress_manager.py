import asyncio
import logging
from typing import Optional, Callable, Any, Dict, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ProgressPhase(Enum):
    """Enum for different progress phases"""
    DOWNLOADING = "downloading"
    EXTRACTING_AUDIO = "extracting_audio"
    CONVERTING_AUDIO = "converting_audio"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    FINALIZING = "finalizing"


@dataclass
class PhaseConfig:
    """Configuration for a progress phase"""
    base_message: str  # Base message without dots
    base_delay: float  # Base delay for fake checkpoints in seconds
    use_fake_checkpoints: bool = True  # Whether to use fake checkpoints
    dots_interval: float = 5.0  # Interval for dots animation in seconds


class DynamicProgressManager:
    """
    Manages dynamic progress notifications with animated dots and fake checkpoints.

    Features:
    1. Animated dots that cycle every few seconds
    2. Fake checkpoints with exponentially increasing delays
    3. Real checkpoints override fake ones
    4. Easy configuration per phase
    """

    def __init__(self, message, progress_bar_func, i18n, session_id: Optional[str] = None):
        """
        Initialize the progress manager.

        Args:
            message: Telegram message to update
            progress_bar_func: Function to generate progress bar
            i18n: Internationalization object
        """
        self.message = message
        self.progress_bar_func = progress_bar_func
        self.i18n = i18n
        self.session_id: Optional[str] = session_id

        # State tracking
        self.current_phase: Optional[ProgressPhase] = None
        self.current_progress: int = 0
        self.is_running: bool = False
        self.animation_task: Optional[asyncio.Task] = None
        self.fake_checkpoint_task: Optional[asyncio.Task] = None

        # Concurrency control for message edits
        self._edit_lock = asyncio.Lock()

        # Error handling
        self.consecutive_errors: int = 0
        self.max_consecutive_errors: int = 5

        # Animation state
        self.dots_count: int = 0
        self.max_dots: int = 3

        # Fake checkpoint state
        self.fake_checkpoint_delay: float = 0
        self.fake_checkpoint_count: int = 0

        # Phase configurations
        self.phase_configs: Dict[ProgressPhase, PhaseConfig] = {
            ProgressPhase.DOWNLOADING: PhaseConfig(
                base_message="downloading_file",
                base_delay=3.0,
                use_fake_checkpoints=False  # Downloads are usually quick
            ),
            ProgressPhase.EXTRACTING_AUDIO: PhaseConfig(
                base_message="getting_audio",
                base_delay=5.0,
                use_fake_checkpoints=False  # Audio extraction is usually quick
            ),
            ProgressPhase.CONVERTING_AUDIO: PhaseConfig(
                base_message="convert_audio",
                base_delay=4.0,
                use_fake_checkpoints=False  # Conversion is usually quick
            ),
            ProgressPhase.TRANSCRIBING: PhaseConfig(
                base_message="transcribe_audio_progress",
                base_delay=10.0,  # Base delay for transcription
                use_fake_checkpoints=True  # Transcription can be long
            ),
            ProgressPhase.SUMMARIZING: PhaseConfig(
                base_message="summarise_text",
                base_delay=8.0,  # Base delay for summarization
                use_fake_checkpoints=False  # Summarization can be long
            ),
            ProgressPhase.FINALIZING: PhaseConfig(
                base_message="send_result",
                base_delay=2.0,
                use_fake_checkpoints=False  # Finalizing is usually quick
            )
        }

    async def start_phase(self, phase: ProgressPhase, initial_progress: int, audio_duration: Optional[float] = None):
        """
        Start a new progress phase.

        Args:
            phase: The progress phase to start
            initial_progress: Initial progress percentage
            audio_duration: Audio duration for calculating delays (optional)
        """
        # Stop any existing tasks
        await self.stop()

        old_phase = self.current_phase.value if self.current_phase else "None"
        old_progress = self.current_progress

        self.current_phase = phase
        self.current_progress = initial_progress
        self.is_running = True
        self.dots_count = 0
        self.fake_checkpoint_count = 0

        # Calculate base delay based on audio duration if provided
        config = self.phase_configs[phase]
        original_base_delay = config.base_delay

        if audio_duration and config.use_fake_checkpoints:
            # Adjust base delay based on audio length
            # For short audio (< 60s): use base delay
            # For longer audio: scale the delay
            if audio_duration < 300:
                self.fake_checkpoint_delay = 5
                delay_multiplier = '5 sec (<5 minutes audio)'
            elif 300 < audio_duration < 900:
                self.fake_checkpoint_delay = audio_duration / 45
                delay_multiplier = '45x (5-15 minutes audio)'
            elif 900 < audio_duration < 1800:
                self.fake_checkpoint_delay = audio_duration / 63
                delay_multiplier = '63x (15-30 minutes audio)'
            elif 1800 < audio_duration < 3600:
                self.fake_checkpoint_delay = audio_duration / 138
                delay_multiplier = '138x (30-60 minutes audio)'
            elif audio_duration > 3600:
                self.fake_checkpoint_delay = audio_duration / 250
                delay_multiplier = '250x (>60 minutes audio)'
            else:
                self.fake_checkpoint_delay = audio_duration / 10
                delay_multiplier = '10x (60-90 minutes audio)'
        else:
            self.fake_checkpoint_delay = config.base_delay
            delay_multiplier = "1.0x (no audio duration or fake checkpoints disabled)"

        # Log the phase transition with detailed information
        logger.debug(f"🔄 PHASE CHANGE: {old_phase} ({old_progress}%) → {phase.value} ({initial_progress}%)")

        if audio_duration:
            logger.debug(f"📊 AUDIO INFO: Duration = {audio_duration:.1f}s")

        if config.use_fake_checkpoints:
            logger.debug(f"⏱️  FAKE CHECKPOINT CONFIG: Base delay = {original_base_delay}s, "
                       f"Calculated delay = {self.fake_checkpoint_delay}s ({delay_multiplier}), "
                       f"Dots interval = {config.dots_interval}s")
        else:
            logger.debug(f"⏱️  FAKE CHECKPOINTS: Disabled for phase {phase.value}")

        # Start animation task
        self.animation_task = asyncio.create_task(self._animate_dots())

        # Start fake checkpoint task if enabled
        if config.use_fake_checkpoints:
            self.fake_checkpoint_task = asyncio.create_task(self._fake_checkpoint_loop())

        # Send initial message
        await self._update_message()

        logger.debug(f"✅ Started progress phase {phase.value} at {initial_progress}% (chat_id={getattr(self.message.chat, 'id', None)}, message_id={getattr(self.message, 'message_id', None)}, session_id={self.session_id})")

    async def update_progress(self, new_progress: int, force_real_checkpoint: bool = True):
        """
        Update progress to a new value (real checkpoint).

        Args:
            new_progress: New progress percentage
            force_real_checkpoint: Whether this is a real checkpoint that should override fake ones
        """
        if not self.is_running:
            logger.debug(f"⚠️  Progress update ignored (not running): {new_progress}%")
            return

        old_progress = self.current_progress
        self.current_progress = new_progress
        phase_name = self.current_phase.value if self.current_phase else "Unknown"

        if force_real_checkpoint:
            # Reset fake checkpoint counter when real checkpoint occurs
            old_fake_count = self.fake_checkpoint_count
            self.fake_checkpoint_count = 0
            checkpoint_info = f"REAL checkpoint (reset {old_fake_count} fake checkpoints)"
        else:
            checkpoint_info = "progress update"

        await self._update_message()

        logger.debug(f"📈 PROGRESS UPDATE [{phase_name}]: {old_progress}% → {new_progress}% ({checkpoint_info})")

    async def stop(self):
        """Stop all progress tasks."""
        if not self.is_running:
            logger.debug("🛑 Stop called but progress manager already stopped")
            return

        phase_name = self.current_phase.value if self.current_phase else "Unknown"
        final_progress = self.current_progress
        total_fake_checkpoints = self.fake_checkpoint_count

        logger.debug(f"🛑 STOPPING PROGRESS MANAGER [{phase_name}]: "
                   f"Final progress = {final_progress}%, "
                   f"Total fake checkpoints = {total_fake_checkpoints}")

        self.is_running = False

        # Cancel animation task
        if self.animation_task and not self.animation_task.done():
            logger.debug(f"🎭 Cancelling dots animation task...")
            self.animation_task.cancel()
            try:
                await self.animation_task
            except asyncio.CancelledError:
                pass

        # Cancel fake checkpoint task
        if self.fake_checkpoint_task and not self.fake_checkpoint_task.done():
            logger.debug(f"🎯 Cancelling fake checkpoint task...")
            self.fake_checkpoint_task.cancel()
            try:
                await self.fake_checkpoint_task
            except asyncio.CancelledError:
                pass

        logger.debug(f"✅ Progress manager stopped [{phase_name}] (chat_id={getattr(self.message.chat, 'id', None)}, message_id={getattr(self.message, 'message_id', None)}, session_id={self.session_id})")

    async def _animate_dots(self):
        """Animate the dots in the progress message."""
        try:
            phase_name = self.current_phase.value if self.current_phase else "Unknown"
            config = self.phase_configs[self.current_phase]

            logger.debug(f"🎭 DOTS ANIMATION STARTED [{phase_name}]: Interval = {config.dots_interval}s")

            while self.is_running:
                await asyncio.sleep(config.dots_interval)

                if not self.is_running:
                    break

                # Update dots count
                old_dots = self.dots_count
                self.dots_count = (self.dots_count + 1) % (self.max_dots + 1)
                if self.dots_count == 0:
                    self.dots_count = 1  # Start from 1 dot, not 0

                logger.debug(f"🎭 DOTS ANIMATION [{phase_name}]: {old_dots} → {self.dots_count} dot(s)")
                await self._update_message()
        except asyncio.CancelledError:
            logger.debug(f"🛑 Dots animation cancelled [{phase_name if 'phase_name' in locals() else 'Unknown'}]")
            pass
        except Exception as e:
            logger.error(f"❌ Error in dots animation [{phase_name if 'phase_name' in locals() else 'Unknown'}]: {e}")

    async def _fake_checkpoint_loop(self):
        """Handle fake checkpoints with exponential backoff."""
        try:
            current_delay = self.fake_checkpoint_delay
            phase_name = self.current_phase.value if self.current_phase else "Unknown"

            logger.debug(f"🔄 FAKE CHECKPOINT LOOP STARTED [{phase_name}]: Initial delay = {current_delay}s")

            while self.is_running:
                logger.debug(f"⏳ Waiting {current_delay}s for next fake checkpoint [{phase_name}]...")
                await asyncio.sleep(current_delay)

                if not self.is_running:
                    logger.debug(f"🛑 Fake checkpoint loop stopped [{phase_name}]")
                    break

                # Move progress forward - use specific values for transcription phase
                if self.current_phase == ProgressPhase.TRANSCRIBING:
                    new_progress = self._get_next_transcription_checkpoint()
                else:
                    # For other phases, use the old logic
                    fake_progress_increment = min(5, max(2, (95 - self.current_progress) // 8))
                    new_progress = min(95, self.current_progress + fake_progress_increment)

                # Only update if we can actually move forward
                if new_progress > self.current_progress:
                    old_progress = self.current_progress
                    self.current_progress = new_progress
                    self.fake_checkpoint_count += 1
                    await self._update_message()

                    next_delay = current_delay * 2
                    increment = new_progress - old_progress
                    logger.debug(f"🎯 FAKE CHECKPOINT #{self.fake_checkpoint_count} [{phase_name}]: "
                               f"{old_progress}% → {new_progress}% "
                               f"(increment: +{increment}%, next delay: {next_delay}s)")
                else:
                    logger.debug(f"⚠️  Fake checkpoint skipped [{phase_name}]: "
                                f"Cannot progress from {self.current_progress}% (would be {new_progress}%)")

                # Double the delay for next fake checkpoint
                current_delay *= 2

        except asyncio.CancelledError:
            logger.debug(f"🛑 Fake checkpoint loop cancelled [{phase_name if 'phase_name' in locals() else 'Unknown'}]")
            pass
        except Exception as e:
            logger.error(f"❌ Error in fake checkpoint loop [{phase_name if 'phase_name' in locals() else 'Unknown'}]: {e}")

    async def _update_message(self):
        """Update the progress message with current state."""
        try:
            if not self.current_phase:
                logger.debug("⚠️  Message update skipped: No current phase")
                return

            config = self.phase_configs[self.current_phase]
            base_message_key = config.base_message
            phase_name = self.current_phase.value

            # Generate dots
            dots = "." * self.dots_count

            # Get the progress bar (using the same function as in the existing codebase)
            progress_bar = self.progress_bar_func(self.current_progress, self.i18n)

            # Use the i18n method calls with dots and progress parameters
            if base_message_key == "downloading_file":
                text = self.i18n.downloading_file(dots=dots, progress=progress_bar)
            elif base_message_key == "getting_audio":
                text = self.i18n.getting_audio(dots=dots, progress=progress_bar)
            elif base_message_key == "convert_audio":
                text = self.i18n.convert_audio(dots=dots, progress=progress_bar)
            elif base_message_key == "transcribe_audio_progress":
                # Use specific messages based on progress level during transcription
                text = self._get_transcription_message(progress_bar, dots)
            elif base_message_key == "summarise_text":
                text = self.i18n.summarise_text(dots=dots, progress=progress_bar)
            elif base_message_key == "send_result":
                text = self.i18n.send_result(dots=dots, progress=progress_bar)
            else:
                # Fallback for unknown message keys
                text = f"{base_message_key.replace('_', ' ').title()}{dots} {progress_bar}"

            # Log the message update attempt
            logger.debug(f"💬 MESSAGE UPDATE [{phase_name}]: '{text[:50]}{'...' if len(text) > 50 else ''}' "
                        f"(progress: {self.current_progress}%, dots: {self.dots_count}, chat_id={getattr(self.message.chat, 'id', None)}, message_id={getattr(self.message, 'message_id', None)}, session_id={self.session_id})")

            # Serialize concurrent edits
            async with self._edit_lock:
                await self.message.edit_text(text=text)

            # Reset error counter on success
            if self.consecutive_errors != 0:
                self.consecutive_errors = 0

        except Exception as e:
            phase_name = self.current_phase.value if self.current_phase else "Unknown"
            # Ignore "message is not modified" errors
            error_text = str(e)
            if "message is not modified" not in error_text:
                chat_id = getattr(self.message.chat, 'id', None)
                msg_id = getattr(self.message, 'message_id', None)
                logger.warning(
                    f"❌ Failed to update progress message [{phase_name}], {self.current_progress}: {e} "
                    f"(chat_id={chat_id}, message_id={msg_id}, session_id={self.session_id})"
                )

                # Auto-stop on specific not-found error
                if "message to edit not found" in error_text.lower() or "message to edit not found" in error_text:
                    try:
                        await self.stop()
                    finally:
                        return

                # Increment consecutive error counter and stop if exceeds threshold
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.max_consecutive_errors:
                    logger.warning(
                        f"🛑 Stopping progress manager due to consecutive errors = {self.consecutive_errors} "
                        f"(phase={phase_name}, chat_id={chat_id}, message_id={msg_id}, session_id={self.session_id})"
                    )
                    await self.stop()
            else:
                logger.debug(f"⚠️  Message not modified [{phase_name}]: {str(e)[:100]}")

    def _get_transcription_message(self, progress_bar: str, dots: str) -> str:
        """
        Get specific transcription message based on current progress level.

        Args:
            progress_bar: The progress bar string
            dots: The animated dots string

        Returns:
            Formatted message for current progress level
        """
        progress = self.current_progress

        # Choose message based on progress level

        if progress <= 39:
            return self.i18n.transcribe_audio_progress(dots=dots, progress=progress_bar)
        elif progress < 85:
            try:
                new_message = self.i18n.get(f"transcribe_fake_{progress}", dots=dots, progress=progress_bar)
                if new_message is not None:
                    return new_message
            except Exception as e:
                logger.error(f"Error getting transcription message for progress {progress}: {e}")
                return self.i18n.transcribe_audio_progress(dots=dots, progress=progress_bar)
        elif progress == 85:
            try:
                return self.i18n.summarize_final_85(dots=dots, progress=progress_bar)
            except:
                return self.i18n.summarise_text(dots=dots, progress=progress_bar)
        else:
            # For any other progress levels, use the basic transcription message
            return self.i18n.transcribe_audio_progress(dots=dots, progress=progress_bar)

    def _get_next_transcription_checkpoint(self) -> int:
        """
        Get the next transcription checkpoint progress value.

        Returns:
            Next progress value for transcription phase
        """
        current = self.current_progress

        # Predefined checkpoint sequence for transcription phase
        checkpoints = [42, 47, 52, 58, 64, 70, 75, 80, 85]

        # Find the next checkpoint
        for checkpoint in checkpoints:
            if checkpoint > current:
                return checkpoint

        # If we're past all checkpoints, just increment by a small amount
        return min(95, current + 2)

    def _safe_get_i18n_message(self, message_key: str, progress_bar: str, dots: str) -> str:
        """
        Safely get i18n message with fallback.

        Args:
            message_key: The i18n message key
            progress_bar: The progress bar string
            dots: The animated dots string

        Returns:
            Formatted message or fallback
        """
        try:
            if hasattr(self.i18n, message_key):
                message_func = getattr(self.i18n, message_key)
                if callable(message_func):
                    result = message_func(dots=dots, progress=progress_bar)
                    if result is not None:
                        return result
                    else:
                        logger.warning(f"i18n message {message_key} returned None")
                else:
                    if message_func is not None:
                        return str(message_func) + dots + f" {progress_bar}"
                    else:
                        logger.warning(f"i18n attribute {message_key} is None")
            else:
                logger.warning(f"i18n message {message_key} not found")
        except Exception as e:
            logger.warning(f"Failed to get i18n message for {message_key}: {e}")

        # Fallback to basic transcription message
        try:
            fallback = self.i18n.transcribe_audio_progress(dots=dots, progress=progress_bar)
            if fallback is not None:
                return fallback
        except Exception as e:
            logger.error(f"Even fallback message failed: {e}")

        # Ultimate fallback
        return f"📝 Processing audio{dots} {progress_bar}"

    def add_custom_phase(self, phase: ProgressPhase, config: PhaseConfig):
        """Add or update a custom phase configuration."""
        self.phase_configs[phase] = config

    def get_current_state(self) -> Dict[str, Any]:
        """Get current state for debugging."""
        return {
            'current_phase': self.current_phase.value if self.current_phase else None,
            'current_progress': self.current_progress,
            'is_running': self.is_running,
            'dots_count': self.dots_count,
            'fake_checkpoint_count': self.fake_checkpoint_count,
            'fake_checkpoint_delay': self.fake_checkpoint_delay
        }


# Convenience functions for common progress phases
async def create_progress_manager(message, progress_bar_func, i18n, session_id: Optional[str] = None) -> DynamicProgressManager:
    """Create a new progress manager instance."""
    return DynamicProgressManager(message, progress_bar_func, i18n, session_id=session_id)


# Phase-specific helper functions
async def start_downloading_phase(manager: DynamicProgressManager, initial_progress: int = 5):
    """Start the downloading phase."""
    await manager.start_phase(ProgressPhase.DOWNLOADING, initial_progress)


async def start_audio_extraction_phase(manager: DynamicProgressManager, initial_progress: int = 20):
    """Start the audio extraction phase."""
    await manager.start_phase(ProgressPhase.EXTRACTING_AUDIO, initial_progress)


async def start_audio_conversion_phase(manager: DynamicProgressManager, initial_progress: int = 30):
    """Start the audio conversion phase."""
    await manager.start_phase(ProgressPhase.CONVERTING_AUDIO, initial_progress)


async def start_transcription_phase(manager: DynamicProgressManager, initial_progress: int = 39, audio_duration: Optional[float] = None):
    """Start the transcription phase with fake checkpoints."""
    await manager.start_phase(ProgressPhase.TRANSCRIBING, initial_progress, audio_duration)


async def start_summarization_phase(manager: DynamicProgressManager, initial_progress: int = 85, audio_duration: Optional[float] = None):
    """Start the summarization phase with fake checkpoints."""
    await manager.start_phase(ProgressPhase.SUMMARIZING, initial_progress, audio_duration)


async def start_finalization_phase(manager: DynamicProgressManager, initial_progress: int = 95):
    """Start the finalization phase."""
    await manager.start_phase(ProgressPhase.FINALIZING, initial_progress)