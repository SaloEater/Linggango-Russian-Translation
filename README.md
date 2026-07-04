Русский перевод для сборки [Linggango](https://www.curseforge.com/minecraft/modpacks/linggango)

В переводе участвовали: @vladyslav2703, @r1ls_

Для установки скачайте последнюю версию из Release и распакуйте в папку со сборкой

These books should be excluded from translation packs because they are translated via regular lang files::
- ars_nouveau\patchouli_books\worn_notebook
- enigmaticlegacy\patchouli_books\the_acknowledgment

How to add translation for your language:
1) Download and install https://www.curseforge.com/minecraft/mc-mods/translations-extractor
2) Download and install https://www.curseforge.com/minecraft/mc-mods/ftb-quest-localizer
3) Run the game
4) Open config/translations_extractor-client.toml
5) Change targetLanguage to your language code
6) Restart the game, load world, run `/extract translations <type>`
7) Go to resourcepacks/ExtractedTranslations_<type>, copy assets and put them here into resourcepacks/<name of resource pack> (Currently they are divided by language)
8) Translate files
9) Commit and push