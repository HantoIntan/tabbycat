import logging

from django.db import models
from django.utils.functional import cached_property
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

from .generator import DRAW_FLAG_DESCRIPTIONS

logger = logging.getLogger(__name__)


class DebateManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super().get_queryset().select_related('round')


class Debate(models.Model):
    STATUS_NONE = 'N'
    STATUS_POSTPONED = 'P'
    STATUS_DRAFT = 'D'
    STATUS_CONFIRMED = 'C'
    STATUS_CHOICES = ((STATUS_NONE, 'None'),
                      (STATUS_POSTPONED, 'Postponed'),
                      (STATUS_DRAFT, 'Draft'),
                      (STATUS_CONFIRMED, 'Confirmed'), )

    objects = DebateManager()

    round = models.ForeignKey('tournaments.Round', db_index=True)
    venue = models.ForeignKey('venues.Venue', blank=True, null=True)
    division = models.ForeignKey('divisions.Division', blank=True, null=True)

    bracket = models.FloatField(default=0)
    room_rank = models.IntegerField(default=0)

    time = models.DateTimeField(
        blank=True, null=True,
        help_text="The time/date of a debate if it is specifically scheduled")

    # comma-separated list of strings
    flags = models.CharField(max_length=100, blank=True, null=True)

    importance = models.IntegerField(default=0)
    result_status = models.CharField(max_length=1,
                                     choices=STATUS_CHOICES,
                                     default=STATUS_NONE)
    ballot_in = models.BooleanField(default=False)

    def __contains__(self, team):
        return team in (self.aff_team, self.neg_team)

    def __str__(self):
        description = "[{}/{}/{}] ".format(self.round.tournament.slug, self.round.abbreviation, self.id)
        try:
            description += self.matchup
        except:
            logger.critical("Error rendering Debate.matchup in Debate.__str__", exc_info=True)
            description += "<error showing teams>"
        return description

    @property
    def matchup(self):
        # This method is used by __str__, so it's not allowed to crash (ever)
        try:
            return "%s vs %s" % (self.aff_team.short_name, self.neg_team.short_name)
        except (ObjectDoesNotExist, MultipleObjectsReturned):
            dts = self.debateteam_set.all()
            if all(dt.position == DebateTeam.POSITION_UNALLOCATED for dt in dts):
                return ", ".join([dt.team.short_name for dt in dts])
            else:
                return ", ".join(["%s (%s)" % (dt.team.short_name, dt.get_position_display().lower())
                    for dt in dts])

    def _populate_teams(self):
        """Populates the team attributes from self.debateteam_set.
        Callers wishing for this to be done in bulk should prefetch the original
        query with 'debateteam_set__team'."""
        dts = self.debateteam_set.all()
        if not dts._prefetch_done:  # uses internal undocumented flag of Django's QuerySet model
            dts = dts.select_related('team')
        self._teams = []
        self._multiple_found = []
        for dt in dts:
            self._teams.append(dt.team)
            if dt.position == DebateTeam.POSITION_AFFIRMATIVE:
                if hasattr(self, '_aff_team'):
                    self._multiple_found.extend(['_aff_team', '_aff_dt'])
                self._aff_team = dt.team
                self._aff_dt = dt
            elif dt.position == DebateTeam.POSITION_NEGATIVE:
                if hasattr(self, '_neg_team'):
                    self._multiple_found.extend(['_neg_team', '_neg_dt'])
                self._neg_team = dt.team
                self._neg_dt = dt

    def _team_property(attr):  # noqa: N805
        """Used for properties that rely on self._populate_teams(). Such
        properties call self._populate_teams() if it hasn't already been called,
        then try to return the correct attribute. If the attribute doesn't work,
        it raises ObjectDoesNotExist, since this is typically a more
        convenient exception to work with than AttributeError.
        """
        @property
        def _property(self):
            if not hasattr(self, '_teams'):
                self._populate_teams()
            if attr in self._multiple_found:
                raise MultipleObjectsReturned("Multiple objects found for attribute '%s' in debate ID %d" % (attr, self.id))
            try:
                return getattr(self, attr)
            except AttributeError:
                raise ObjectDoesNotExist("No object found for attribute '%s' in debate ID %d" % (attr, self.id))
        return _property

    @property
    def teams(self):
        # No need for _team_property overhead, this list is guaranteed to exist
        if not hasattr(self, '_teams'):
            self._populate_teams()
        return self._teams

    aff_team = _team_property('_aff_team')
    neg_team = _team_property('_neg_team')
    aff_dt = _team_property('_aff_dt')
    neg_dt = _team_property('_neg_dt')

    def get_team(self, side):
        return getattr(self, '%s_team' % side)

    def get_dt(self, side):
        """dt = DebateTeam"""
        return getattr(self, '%s_dt' % side)

    def get_side(self, team):
        # Deprecated 12/9/2016, remove after 12/10/2016
        raise RuntimeError("Debate.get_side() is deprecated.")

    @property
    def confirmed_ballot(self):
        """Returns the confirmed BallotSubmission for this debate, or None if
        there is no such ballot submission."""
        try:
            return self._confirmed_ballot
        except AttributeError:
            try:
                self._confirmed_ballot = self.ballotsubmission_set.get(confirmed=True)
            except ObjectDoesNotExist:
                self._confirmed_ballot = None
            return self._confirmed_ballot

    @property
    def flags_all(self):
        if not self.flags:
            return []
        else:
            return [DRAW_FLAG_DESCRIPTIONS[f] for f in self.flags.split(",")]

    @property
    def history(self):
        try:
            return self._history
        except AttributeError:
            self._history = self.aff_team.seen(self.neg_team, before_round=self.round.seq)
            return self._history

    @cached_property
    def draw_conflicts(self):
        d = []
        history = self.history
        if history == 1:
            d.append("Teams have met once")
        elif history == 2:
            d.append("Teams have met twice")
        elif history > 2:
            d.append("Teams have met %d times" % (history,))
        if self.aff_team.institution_id == self.neg_team.institution_id:
            d.append("Teams are from the same institution")

        return d

    @property
    def adjudicators(self):
        """Returns an AdjudicatorAllocation containing the adjudicators for this
        debate."""
        try:
            return self._adjudicators
        except AttributeError:
            from adjallocation.allocation import AdjudicatorAllocation
            self._adjudicators = AdjudicatorAllocation(self, from_db=True)
            return self._adjudicators

    @property
    def get_division_motions(self):
        from motions.models import Motion
        motions = Motion.objects.filter(round=self.round, divisions=self.division)
        if motions.count() > 0:
            return motions[0] # Pretty sure this should never be > 1
        else:
            # Its easiest to assume a division motion is always present, so
            # return a fake one if it is not
            from motions.models import Motion
            return Motion(text='-', reference='-')


class DebateTeamManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super().get_queryset().select_related('debate')


class DebateTeam(models.Model):
    POSITION_AFFIRMATIVE = 'A'
    POSITION_NEGATIVE = 'N'
    POSITION_UNALLOCATED = 'u'
    POSITION_CHOICES = ((POSITION_AFFIRMATIVE, 'Affirmative'),
                        (POSITION_NEGATIVE, 'Negative'),
                        (POSITION_UNALLOCATED, 'Unallocated'), )

    objects = DebateTeamManager()

    debate = models.ForeignKey(Debate, db_index=True)
    team = models.ForeignKey('participants.Team')
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    def __str__(self):
        return '{} in {}'.format(self.team.short_name, self.debate)

    @property
    def opponent(self):
        try:
            return self._opponent
        except AttributeError:
            try:
                self._opponent = DebateTeam.objects.exclude(position=self.position).select_related(
                        'team', 'team__institution').get(debate=self.debate)
            except (DebateTeam.DoesNotExist, DebateTeam.MultipleObjectsReturned):
                logger.warning("No opponent found for %s", str(self))
                self._opponent = None
            return self._opponent

    @property
    def opposition(self):
        # Deprecated 12/9/2016, remove after 12/10/2016
        raise RuntimeError("DebateTeam.opposition is deprecated, use DebateTeam.opponent instead.")

    @property
    def result(self):
        # Deprecated 12/9/2016, remove after 12/10/2016
        raise RuntimeError("DebateTeam.result is deprecated, use DebateTeam.get_result_display() instead.")

    def get_result_display(self):
        if self.win is True:
            return 'won'
        elif self.win is False:
            return 'lost'
        else:
            return 'result unknown'

    @property
    def win(self):
        """Convenience function. Returns True if this team won, False if this
        team lost, or None if there isn't a confirmed result.

        This result is stored for the lifetime of the instance -- it won't
        update on the same instance if a result is entered."""
        try:
            return self._win
        except AttributeError:
            try:
                self._win = self.teamscore_set.get(ballot_submission__confirmed=True).win
            except ObjectDoesNotExist:
                self._win = None
            return self._win


class TeamPositionAllocation(models.Model):
    """Model to store team position allocations for tournaments like Joynt
    Scroll (New Zealand). Each team-round combination should have one of these.
    In tournaments without team position allocations, just don't use this
    model."""

    POSITION_AFFIRMATIVE = DebateTeam.POSITION_AFFIRMATIVE
    POSITION_NEGATIVE = DebateTeam.POSITION_NEGATIVE
    POSITION_UNALLOCATED = DebateTeam.POSITION_UNALLOCATED
    POSITION_CHOICES = DebateTeam.POSITION_CHOICES

    round = models.ForeignKey('tournaments.Round')
    team = models.ForeignKey('participants.Team')
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    class Meta:
        unique_together = [('round', 'team')]
