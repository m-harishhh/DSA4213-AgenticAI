"""
This file is created to explore the game rules and mechanism 
and test if it is feasible to develop on
Date: 7/9/2026
Harish
"""



from dataclasses import dataclass 
from typing import List
import random


@dataclass
class Evidence: 
    evidence_type: str
    source: str
    subject: str
    location: str | None = None
    weapon: str | None = None 
    time: str | None = None 
    related_person: str | None = None

@dataclass 
class Gamestate:
    suspects: List[str]
    weapons: List[str]
    locations: List[str]

    culprit: str
    murder_weapon: str
    murder_location: str

def generate_solution(suspects, weapons, locations):
    culprit = random.choice(suspects)
    murder_weapon = random.choice(weapons)
    murder_location = random.choice(locations)

    return culprit, murder_weapon, murder_location

def create_game(seed = None) -> Gamestate:
    rng = random.Random(seed)
    suspects = [
        'Apple', 'Bing', 'Chilling', 'Dumb_AF', 'Elephante',
        'Frankenstein', 'GOD', 'Hola', 'IsFake', 'Jackof'
    ]

    weapons = ['Knife', 'Rope', 'Candlestick', 'Pistol', 'Hammer']

    locations = ['Kitchen', 'Library', 'Study', 'Ballroom', 'Garden']

    culprit, muder_weapon, murder_location = generate_solution(suspects, weapons, locations)

    return Gamestate(
        suspects= suspects,
        weapons= weapons,
        locations = locations,
        culprit = culprit, 
        murder_weapon= muder_weapon,
        murder_location = murder_location
    )


e1 = Evidence(
    evidence_type="location",
    source="Bing",
    subject="Apple",
    location="Kitchen",
    time="9pm"
)

e2 = Evidence(
    evidence_type="location",
    source="GOD",
    subject="Apple",
    location="Library",
    time="9pm"
)

e3 = Evidence(
    evidence_type="weapon",
    source="Hola",
    subject="Chilling",
    weapon="Knife"
)

e4 = Evidence(
    evidence_type="alibi",
    source="Frankenstein",
    subject="GOD",
    related_person="Frankenstein",
    time="9pm"
)


def contradicts(e1: Evidence, e2: Evidence) -> bool:
    """
    This is a naive logic and is not fully developed 
    The core logical flow is:
    
    is the subject (suspect) the same in both evidence? (for an evidence to contradict the subj needs to be the same)
    |-> Does both evidence state the same time? 
        |-> Does both evidence state the same location? 
            |-> Does both the evidence state the same murder weapon?

    sample evidence 
    Valid contradiction: here the location is diff
    E1: Apple was at the Kitchen at 9pm
    E2: Apple was at the Library at 9pm   

    Invalid contradiction: here location is diff but is also diff time
    E1: Apple was at the Kitchen at 9pm
    E2: Apple was at the Library at 10pm 
    """
    if e1.subject != e2.subject or e1.time != e2.time:
        return False
    if e1.location != e2.location:
        return True
    if e1.weapon != e2.weapon:
        return True

if __name__ == "__main__":
    game = create_game(seed = 42)
    print(game)