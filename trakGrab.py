# trakGrab.py
# Daniel Guilbert, Nawid Salehie
# 12.11.19 - 24.08.25
# v2.1 - Selenium + Firefox + JSON parsing + safe paths

import os
import re
import json
from urllib.request import urlopen, Request
from pathlib import Path
from bs4 import BeautifulSoup

# Selenium imports for Firefox
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service as FirefoxService

# ------------------ User input ------------------
artist = input("What is the artist name? traktrain.com/")
song = '*'  # input("Which song would you like to download? (* for all) ")

print("Connecting...")

# ------------------ Selenium to fetch full HTML ------------------
try:
    firefox_options = Options()
    firefox_options.add_argument("--headless")  # run in background
    firefox_options.add_argument("--disable-gpu")  # optional

    firefox_service = FirefoxService()  # assumes geckodriver in PATH
    driver = webdriver.Firefox(service=firefox_service, options=firefox_options)
    driver.get("https://traktrain.com/" + artist)
    html = driver.page_source
    driver.quit()
except Exception as e:
    input(f"That artist cannot be found, please try again.\nError: {e}")
    exit()

print("Connected!\n")

# ------------------ Get AWS base URL ------------------
urlmatch = re.compile(r"(.)*var AWS_BASE_URL(.)*")
m = urlmatch.search(html)
if m:
    baseUrl = m.group().split("'")[1]
else:
    print("Could not find AWS base URL. Exiting.")
    exit()

# ------------------ Setup download folder ------------------
pwd = Path.cwd() / "songs" / artist
pwd.mkdir(parents=True, exist_ok=True)

# ------------------ Download single song ------------------
if song != '*':
    try:
        soup = BeautifulSoup(html, 'html.parser')
        track_div = soup.find("div", {"data-player-info": True, "data-player-name": song})
        data = json.loads(track_div['data-player-info'])
        songUrl = baseUrl + data['src']
    except Exception:
        print("That song could not be found, please try again.")
        exit()

    print(f"Downloading '{song}'...")
    req = Request(songUrl)
    req.add_header('Referer', 'https://traktrain.com/')
    req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
    safe_name = re.sub(r'[^\w ]', '', song)
    with open(pwd / f"{safe_name}.mp3", 'wb') as f:
        f.write(urlopen(req).read())

# ------------------ Download all songs ------------------
else:
    soup = BeautifulSoup(html, 'html.parser')
    all_tracks = soup.find_all("div", {"data-player-info": True})

    for track_div in all_tracks:
        try:
            data = json.loads(track_div['data-player-info'])
            song_url = baseUrl + data['src']
            song_name = data['name']

            print(f"Downloading '{song_name}'...")
            req = Request(song_url)
            req.add_header('Referer', 'https://traktrain.com/')
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')

            safe_name = re.sub(r'[^\w ]', '', song_name)
            with open(pwd / f"{safe_name}.mp3", 'wb') as f:
                f.write(urlopen(req).read())

        except Exception as e:
            print(f"Skipping a track due to error: {e}")

print("\nAll songs downloaded!")
