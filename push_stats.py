import json
import requests

BIN_ID = "6af21d0affd5d16053b4807e"
API_KEY = "$2a$10$lupWpV8BB2R3lLe.KGQu4uNn4jw0NpYpg0XRvmbT1i5PvMZTuOAd2"

URL = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
HEADERS = {
    "Content-Type": "application/json",
    "X-Master-Key": API_KEY
}

def did_win_raw(game, p):
    w = p.get("stats", {}).get("win")
    if w is None:
        for t in game.get("teams", []):
            if t.get("teamId") == p.get("teamId"):
                w = t.get("win")
    return w is True or w == "Win"

def compact(game):
    """Retém apenas o estritamente necessário para poupar espaço."""
    idents = []
    for i in game.get("participantIdentities", []):
        pl = i.get("player", {})
        idents.append({"participantId": i["participantId"],
                       "player": {"gameName": pl.get("gameName") or pl.get("summonerName") or ""}})
    parts = []
    for p in game.get("participants", []):
        parts.append({"participantId": p["participantId"], "teamId": p["teamId"],
                      "championId": p.get("championId"),
                      "stats": {"win": did_win_raw(game, p)}})
    return {"gameId": game.get("gameId"), "gameCreation": game.get("gameCreation", 0),
            "gameCreationDate": game.get("gameCreationDate", ""),
            "participants": parts, "participantIdentities": idents}

def upload():
    try:
        with open("customs_raw.json", "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        
        # Compacta os jogos para ficarem bem abaixo dos 100KB
        data = [compact(g) for g in raw_data]
        
        response = requests.put(URL, json=data, headers=HEADERS)
        if response.status_code == 200:
            print("🚀 Estatísticas compactadas e enviadas com sucesso para a nuvem!")
        else:
            print(f"❌ Erro ao enviar: {response.status_code} - {response.text}")
    except FileNotFoundError:
        print("❌ Ficheiro customs_raw.json não encontrado. Corre primeiro o lcu_custom_history.py.")

if __name__ == "__main__":
    upload()