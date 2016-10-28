"""Handles outgoing balls."""
import asyncio

from mpf.core.utility_functions import Util
from mpf.devices.ball_device.ball_count_handler import EjectProcessCounter
from mpf.devices.ball_device.ball_device_state_handler import BallDeviceStateHandler
from mpf.devices.ball_device.incoming_balls_handler import IncomingBall


class EjectRequest:

    """One eject request."""

    def __init__(self, machine):
        self.max_tries = None
        self.eject_timeout = None
        self.target = None
        self.mechanical = None
        self.confirm_future = asyncio.Future(loop=machine.clock.loop)

    def wait_for_eject_confirm(self):
        """Wait for eject confirmation."""
        return self.confirm_future


class OutgoingBallsHandler(BallDeviceStateHandler):

    """Handles all outgoing balls."""

    def __init__(self, ball_device):
        super().__init__(ball_device)
        self._eject_queue = asyncio.Queue(loop=self.machine.clock.loop)

    def add_eject_to_queue(self, eject: EjectRequest):
        """Add an eject request to queue."""
        self._eject_queue.put_nowait(eject)

    @asyncio.coroutine
    def _run(self):
        """Wait for eject queue."""
        while True:
            eject_queue_future = self.ball_device.ensure_future(self._eject_queue.get())
            eject_request = yield from eject_queue_future

            self.debug_log("Got eject request")

            yield from self._ejecting(eject_request)

    @asyncio.coroutine
    def _ejecting(self, eject_request: EjectRequest):
        """Perform main eject loop."""
        # TODO: handle unexpected mechanical eject
        eject_try = 0
        while True:
            # wait until we have a ball (might be instant)
            self.debug_log("Wait for ball")
            yield from self.ball_device.ball_count_handler.wait_for_ball()
            # inform targets about the eject (can delay the eject)
            yield from self._prepare_eject(eject_request)
            # check if we still have a ball
            ball_count = yield from self.ball_device.ball_count_handler.get_ball_count()
            if ball_count == 0:
                # abort the eject because ball was lost in the meantime
                # TODO: might be mechanical eject
                yield from self._abort_eject(eject_request, eject_try)
                # try again
                continue
            self.debug_log("Ejecting ball")
            result = yield from self._eject_ball(eject_request, eject_try)
            if result:
                # eject is done. return to main loop
                return

            yield from self._failed_eject(eject_request, eject_try)
            eject_try += 1

            if eject_request.max_tries and eject_try > eject_request.max_tries:
                # stop device
                self.ball_device.stop()
                # TODO: inform machine about broken device
                return

    @asyncio.coroutine
    def _prepare_eject(self, eject_request: EjectRequest):
        pass

    @asyncio.coroutine
    def _abort_eject(self, eject_request: EjectRequest):
        pass

    @asyncio.coroutine
    def _failed_eject(self, eject_request: EjectRequest):
        pass

    @asyncio.coroutine
    def _eject_ball(self, eject_request: EjectRequest, eject_try) -> bool:
        # inform the counter that we are ejecting now
        ball_eject_process = self.ball_device.ball_count_handler.start_eject()
        self.debug_log("Wait for ball to leave device")
        # eject the ball
        self.ball_device.ejector.eject_one_ball(ball_eject_process.is_jammed(), eject_try)
        # wait until the ball has left
        timeout = eject_request.eject_timeout
        try:
            yield from Util.first([ball_eject_process.wait_for_ball_left()], timeout=timeout,
                                  loop=self.machine.clock.loop)
        except asyncio.TimeoutError:
            # timeout. ball did not leave. failed
            ball_eject_process.eject_failed()
            return False
        else:
            self.debug_log("Ball left")
            incoming_ball_at_target = self._add_incoming_ball_to_target(eject_request)
            return (yield from self._handle_confirm(eject_request, ball_eject_process, incoming_ball_at_target))

    def _add_incoming_ball_to_target(self, eject_request: EjectRequest) -> IncomingBall:
        incoming_ball_at_target = IncomingBall()
        # we are the source of this ball
        incoming_ball_at_target.source = self.ball_device
        # there is no timeout
        incoming_ball_at_target.timeout_future = asyncio.Future(loop=self.machine.clock.loop)
        # we will wait on this future
        incoming_ball_at_target.confirm_future = eject_request.confirm_future
        eject_request.target.add_incoming_ball(incoming_ball_at_target)
        return incoming_ball_at_target

    @asyncio.coroutine
    def _handle_confirm(self, eject_request: EjectRequest, ball_eject_process: EjectProcessCounter,
                        incoming_ball_at_target: IncomingBall) -> bool:
        # TODO: check double eject
        self.debug_log("Wait for confirm")
        timeout = eject_request.eject_timeout
        try:
            yield from Util.first([eject_request.wait_for_eject_confirm()], timeout=timeout,
                                  loop=self.machine.clock.loop, cancel_others=False)
        except asyncio.TimeoutError:
            self.debug_log("Got timeout before confirm")
            # ball did not get confirmed
            if ball_eject_process.is_ball_returned():
                # ball returned. eject failed
                ball_eject_process.eject_failed()
                return False
            return (yield from self._handle_late_confirm_or_missing(eject_request, ball_eject_process))
        else:
            # eject successful
            self.debug_log("Got eject confirm")
            ball_eject_process.eject_done()
            return True

    @asyncio.coroutine
    def _handle_late_confirm_or_missing(self, eject_request: EjectRequest, ball_eject_process: EjectProcessCounter)\
            -> bool:
        ball_return_future = ball_eject_process.wait_for_ball_return()
        eject_success_future = eject_request.wait_for_eject_confirm()
        timeout = 60    # TODO: make this dynamic

        # TODO: timeout
        try:
            event = yield from Util.first([ball_return_future, eject_success_future], timeout=timeout,
                                          loop=self.machine.clock.loop)
        except asyncio.TimeoutError:
            # handle lost ball
            raise AssertionError("handle lost ball")
        else:
            if event == eject_success_future:
                # we eventually got eject success
                ball_eject_process.eject_done()
                return True
            elif event == ball_return_future:
                # ball returned. eject failed
                ball_eject_process.eject_failed()
                return False
            else:
                raise AssertionError("Invalid state")
