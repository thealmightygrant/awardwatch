import math

# U.S. AmEx Membership Rewards airline programs directly queryable through
# Seats.aero's cached API, plus United MileagePlus.
#
# Some current AmEx airline partners (Aer Lingus, British Airways, Iberia, ANA,
# and Cathay Pacific) are not exposed as direct Seats.aero cached API sources,
# so they are not included in this automated data feed.
PROGRAMS = {
    "aeroplan": {
        "name": "Air Canada Aeroplan",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "flyingblue": {
        "name": "Air France/KLM Flying Blue",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "lifemiles": {
        "name": "Avianca LifeMiles",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "virginatlantic": {
        "name": "Virgin Atlantic Flying Club",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "delta": {
        "name": "Delta SkyMiles",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "emirates": {
        "name": "Emirates Skywards",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.25,
        "has_seat_count": False,
    },
    "qantas": {
        "name": "Qantas Frequent Flyer",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "qatar": {
        "name": "Qatar Airways Privilege Club",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "singapore": {
        "name": "Singapore KrisFlyer",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "aeromexico": {
        "name": "Aeromexico Rewards",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 0.625,
        "has_seat_count": True,
    },
    "jetblue": {
        "name": "JetBlue TrueBlue",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.25,
        "has_seat_count": True,
    },
    "united": {
        "name": "United MileagePlus",
        "funding": "United miles",
        "mr_per_program_point": None,
        "has_seat_count": True,
    },
}

def approx_mr(points, source):
    if points is None:
        return None
    ratio = PROGRAMS[source]["mr_per_program_point"]
    if ratio is None:
        return None
    return int(math.ceil((points * ratio) / 1000.0) * 1000)

def effective_cost(points, source):
    mr = approx_mr(points, source)
    return mr if mr is not None else points

def under_limit(points, source, limit=100_000):
    cost = effective_cost(points, source)
    return cost is not None and cost < limit
