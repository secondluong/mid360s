"""Early detection of a runaway Point-LIO pose.

Field case: over a Pangolin VPN the trajectory walked a corridor normally and
then left as a dead-straight line that never came back. Reproduced on the board
by loading the A76 cores: Linux migrated laserMapping to an A55, odom fell from
13.7 Hz to 4.4 Hz, the lidar constraint starved and the state propagated on IMU
alone — 539 m in 10 s with the board sitting still, net displacement equal to
path length to four figures.

The existing guard is POSE_MAX_M, a 5 km radius. It does fire eventually, but
at 54 m/s that is ~93 s of ruined data, and a slower runaway never reaches it at
all. Speed is scale-free, so these tests pin that property: non-physical speed
is caught within a few frames however far the pose has travelled, and ordinary
walking never trips it.
"""

import pytest

import server


@pytest.fixture
def hub():
    """A bare object carrying just the speed-tracking state.

    Constructing a real Hub would pull in the voxel map and the saved-map load;
    the control law under test only touches these fields.
    """

    class Bare:
        note_pose_speed = server.Hub.note_pose_speed
        clear_pose_speed = server.Hub.clear_pose_speed

        def __init__(self):
            self.pose_speed = 0.0
            self.pose_speed_max = 0.0
            self.speed_warn = False
            self.speed_warn_frames = 0
            self._speed_hits = 0

    return Bare()


WALK = 1.5  # m/s, a carried board
RUNAWAY = 53.9  # m/s, the speed actually measured while diverged


def feed(hub, speed, n):
    """Returns how many frames reported a fresh warning."""
    return sum(bool(hub.note_pose_speed(speed)) for _ in range(n))


def test_walking_never_warns(hub):
    assert feed(hub, WALK, 200) == 0
    assert hub.speed_warn is False
    assert hub.speed_warn_frames == 0


def test_running_never_warns(hub):
    """The threshold has to clear a sprint, not just a walk."""
    assert feed(hub, 5.5, 100) == 0
    assert hub.speed_warn is False


def test_runaway_warns(hub):
    assert feed(hub, RUNAWAY, 20) == 1, "one fresh warning, not one per frame"
    assert hub.speed_warn is True


def test_warns_after_exactly_the_configured_hits(hub):
    """Caught in a few frames — the whole point versus the 5 km radius bound."""
    for _ in range(server.SPEED_WARN_HITS - 1):
        assert hub.note_pose_speed(RUNAWAY) is False
        assert hub.speed_warn is False
    assert hub.note_pose_speed(RUNAWAY) is True
    assert hub.speed_warn is True


def test_single_frame_jump_does_not_warn(hub):
    """The estimator emits the odd one-frame jump; that is not a runaway."""
    for _ in range(10):
        hub.note_pose_speed(WALK)
        assert hub.note_pose_speed(RUNAWAY) is False
    assert hub.speed_warn is False


def test_interrupted_run_restarts_the_count(hub):
    hub.note_pose_speed(RUNAWAY)
    hub.note_pose_speed(RUNAWAY)
    hub.note_pose_speed(WALK)  # breaks the streak
    assert feed(hub, RUNAWAY, server.SPEED_WARN_HITS - 1) == 0
    assert hub.speed_warn is False


def test_warning_clears_when_speed_returns(hub):
    feed(hub, RUNAWAY, 20)
    assert hub.speed_warn is True
    hub.note_pose_speed(WALK)
    assert hub.speed_warn is False


def test_warning_can_fire_again_after_clearing(hub):
    assert feed(hub, RUNAWAY, 20) == 1
    hub.note_pose_speed(WALK)
    assert feed(hub, RUNAWAY, 20) == 1


def test_peak_speed_is_kept_after_recovery(hub):
    """Point-LIO cannot recover on its own, so a peak with the warning cleared
    is evidence it was restarted — worth keeping for the operator."""
    feed(hub, RUNAWAY, 20)
    feed(hub, WALK, 20)
    assert hub.speed_warn is False
    assert hub.pose_speed == pytest.approx(WALK)
    assert hub.pose_speed_max == pytest.approx(RUNAWAY)


def test_frames_counted_only_while_non_physical(hub):
    feed(hub, WALK, 50)
    assert hub.speed_warn_frames == 0
    feed(hub, RUNAWAY, 7)
    assert hub.speed_warn_frames == 7


def test_clear_forgets_the_streak_but_not_the_peak(hub):
    """A Point-LIO restart re-origins the pose: the step across it is not
    motion, so the streak must not survive it."""
    feed(hub, RUNAWAY, 20)
    hub.clear_pose_speed()
    assert hub.speed_warn is False
    assert hub.pose_speed_max == pytest.approx(RUNAWAY)
    assert feed(hub, RUNAWAY, server.SPEED_WARN_HITS - 1) == 0


def test_threshold_sits_between_a_walk_and_the_runaway():
    """Guards the margin the whole design rests on."""
    assert WALK * 2 < server.POSE_MAX_SPEED_MS < RUNAWAY / 4


def test_speed_beats_the_radius_bound_on_time():
    """At the measured runaway speed, the radius bound needs over a minute
    while the speed gate needs well under a second."""
    radius_s = server.POSE_MAX_M / RUNAWAY
    speed_s = server.SPEED_WARN_HITS / 10.0  # odom runs at 10 Hz
    assert radius_s > 60
    assert speed_s < 1.0
