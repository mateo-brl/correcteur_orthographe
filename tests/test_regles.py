"""Règles du correcteur (engines/regles.py), une à une, avec le vrai dictionnaire de Grammalecte : ce que
chacune corrige, et les phrases voisines, justes, qu'aucune ne doit toucher."""

from __future__ import annotations

import pytest

from conftest import needs_grammalecte

pytestmark = needs_grammalecte


@pytest.fixture(scope="module")
def lex():
    from correcteur.config import Settings
    from correcteur.engines import regles
    from correcteur.engines.grammalecte_engine import GrammalecteEngine

    return regles.Lexique(GrammalecteEngine(Settings())._ensure_loaded().getSpellChecker())


def fautes(text, lex):
    from correcteur.engines import regles

    return regles.check(text, lex, strict=True)


FIXES = [
    # (phrase, mot fautif, correction, règle)
    ("Ils se sont parlés hier.", "parlés", "parlé", "corr_ppas_pronominal_indirect"),
    ("Elle s'est permise de répondre.", "permise", "permis", "corr_ppas_pronominal_indirect"),
    ("Elles se sont regardé dans le miroir.", "regardé", "regardées", "corr_ppas_pronominal_sujet"),
    ("Elle s'est assit sur le banc.", "assit", "assise", "corr_ppas_pronominal_sujet"),
    ("Il faut que je fait mes courses.", "fait", "fasse", "corr_subjonctif"),
    ("Bien qu'il n'est pas là, on commence.", "n'est", "ne soit", "corr_subjonctif"),
    ("Si j'aurais su, je serais venu.", "aurais", "avais", "corr_si_imparfait"),
    ("Ils mange au restaurant.", "mange", "mangent", "corr_conj_sujet"),
    ("Il pensai à elle.", "pensai", "pensait", "corr_conj_sujet"),
    ("Les enfants mange dehors.", "mange", "mangent", "corr_conj_sujet"),
    ("Vous allé bien ?", "allé", "allez", "corr_conj_sujet"),
    ("Je pense qu'il viens demain.", "viens", "vient", "corr_conj_sujet"),
    ("Il ces levé tôt.", "ces", "s'est", "corr_sest"),
    ("Mes parents son partis.", "son", "sont", "corr_son_on_pluriel"),
    ("Les enfants on faim.", "on", "ont", "corr_son_on_pluriel"),
    ("Les gens qui on vu le film sont contents.", "on", "ont", "corr_son_on_pluriel"),
    ("Il ce lave les mains.", "ce", "se", "corr_ce_se"),
    ("Je pense que ces vrai.", "ces", "c'est", "corr_ces_cest"),
    ("C'est enfants sont sages.", "C'est", "Ces", "corr_cest_ces"),
    ("Mais amis sont venus.", "Mais", "Mes", "corr_mais_mes"),
    ("Merci a tous.", "a", "à", "corr_a_preposition"),
    ("Il y a du monde a la fête.", "a", "à", "corr_a_preposition"),
    ("Elle offre des fleurs a sa mère.", "a", "à", "corr_a_preposition"),
    ("La réunion à lieu demain.", "à", "a", "corr_a_verbe"),
    ("Le film à commencé.", "à", "a", "corr_a_verbe"),
    ("Je ne sais pas ou il est.", "ou", "où", "corr_ou_lieu"),
    ("Je ne sais plus ou j'ai mis mes clés.", "ou", "où", "corr_ou_lieu"),
    ("Dis-moi ou tu es.", "ou", "où", "corr_ou_lieu"),
    ("Il en reste peut.", "peut", "peu", "corr_peu"),
    ("Elle reviendra sous peut.", "peut", "peu", "corr_peu"),
    ("Elle est près à partir.", "près", "prête", "corr_pret"),
    ("Les enfants sont près à partir.", "près", "prêts", "corr_pret"),
    ("C'est a dire que non.", "C'est a dire", "C'est-à-dire", "corr_c_est_a_dire"),
    ("Il ni a rien.", "ni", "n'y", "corr_ni_ny"),
    ("Je ni crois pas.", "ni", "n'y", "corr_ni_ny"),
    ("Je vous pris de m'excuser.", "pris", "prie", "corr_prie"),
    ("Il a était malade.", "était", "été", "corr_avoir_ete"),
    ("Je les aient vus.", "aient", "ai", "corr_je_ai"),
    ("Quel heure est-il ?", "Quel", "Quelle", "corr_quel_accord"),
    ("Il a pris sont manteau.", "sont", "son", "corr_sont_son"),
    ("Les fleurs que j'ai cueilli sont fanées.", "cueilli", "cueillies", "corr_ppas_cod_avant"),
    ("Il fesait beau.", "fesait", "faisait", "corr_orthographe"),
    ("Sa fait longtemps.", "Sa", "Ça", "corr_ca"),
    ("Ces gens la sont bizarres.", " la", "-là", "corr_demonstratif_la"),
    ("Leur enfants sont grands.", "Leur", "Leurs", "corr_leurs"),
    ("Ils sont partis en vacance.", "vacance", "vacances", "corr_locution"),
    ("Est-ce-que tu viens ?", "-", " ", "corr_locution"),
    ("Il sans va demain.", "sans", "s'en", "corr_s_en"),
    ("Y a t'il quelqu'un ?", " t'", "-t-", "corr_t_euphonique"),
    ("Que fait t'il ?", " t'", "-", "corr_t_euphonique"),
    ("Ils faut partir.", "Ils", "Il", "corr_il_faut"),
    ("Elle mes donné un livre.", "mes", "m'a", "corr_m_a"),
    ("Il y a beaucoup de gens qui pense comme toi.", "pense", "pensent", "corr_conj_sujet"),
    ("Sa mère et son père son venus.", "son", "sont", "corr_son_on_pluriel"),
    ("Nous nous sommes promené au bord du lac.", "promené", "promenés", "corr_ppas_pronominal_sujet"),
    ("Elle s'est souvenu de tout.", "souvenu", "souvenue", "corr_ppas_pronominal_sujet"),
    ("Ils se sont succédés.", "succédés", "succédé", "corr_ppas_pronominal_indirect"),
    ("Elle à reçu une lettre.", "à", "a", "corr_a_verbe"),
    ("Je les ai mit au frigo.", "mit", "mis", "corr_avoir_participe"),
    ("Il a prit le train.", "prit", "pris", "corr_avoir_participe"),
    ("Je serai la demain.", "la", "là", "corr_la_lieu"),
    ("Ils se sont mit à courir.", "mit", "mis", "corr_ppas_pronominal_sujet"),
    ("Vingt mille lieues sous là mer.", "là", "la", "corr_la_article"),
    ("Il faut manger a la cantine.", "a", "à", "corr_a_preposition"),
    ("Ne fais rien jusqu'a demain.", "a", "à", "corr_locution"),
    ("Vous allez la lui donné.", "donné", "donner", "corr_infinitif"),
    ("Obéis à tous ce qu'on te dit.", "tous", "tout", "corr_locution"),
    ("Et-il arrivé ?", "Et", "Est", "corr_locution"),
    ("Il se sentai de nouveau bien.", "sentai", "sentait", "corr_conj_sujet"),
    ("Elle se querellent sans cesse.", "Elle", "Elles", "corr_conj_sujet"),
    ("S'est une vertu rare.", "S'", "C'", "corr_c_est"),
    ("Allez voir ce que s'est.", "s'", "c'", "corr_c_est"),
    ("C'étais il y a longtemps.", "étais", "était", "corr_c_est"),
    ("Sont élégance est rare.", "Sont", "Son", "corr_sont_son"),
    ("Il risque de voir sont travail volé.", "sont", "son", "corr_sont_son"),
    ("Elles se son succédé.", "son", "sont", "corr_son_on_pluriel"),
    ("Est j'ai des gens en main.", "Est", "Et", "corr_et"),
    ("Des petites zone favorables.", "zone", "zones", "corr_pluriel_nom"),
    ("Rien de particulié.", "particulié", "particulier", "corr_orthographe"),
    ("Ça se conçoi.", "conçoi", "conçoit", "corr_conj_sujet"),
    ("Le ministre de l'Intérieur à estimé que tout va bien.", "à", "a", "corr_a_verbe"),
    ("Nous avons à cherché des issues.", "cherché", "chercher", "corr_a_participe"),
    ("Il ne nous reste qu'à démontré le contraire.", "démontré", "démontrer", "corr_a_participe"),
    ("Je suis contant de te voir.", "contant", "content", "corr_content"),
    ("Je ne suis pas vraiment contant.", "contant", "content", "corr_content"),
    ("Un enfant si contant.", "contant", "content", "corr_content"),
    ("Elle est contante.", "contante", "contente", "corr_orthographe"),
    ("Cela lui a fait du tord.", "tord", "tort", "corr_locution"),
    ("Il a des tords envers elle.", "tords", "torts", "corr_locution"),
    # Rapport de test de la 0.1.15 : « a » après un groupe nominal sujet et son verbe, ou « avoir soif ».
    ("Mes cousins ont une maison a la campagne depuis 21 ans.", "a", "à", "corr_a_preposition"),
    ("Les promeneurs ont soif a la fontaine.", "a", "à", "corr_a_preposition"),
    ("Mon oncle a une maison a la campagne.", "a", "à", "corr_a_preposition"),
    ("C'est lui qui a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Je ne sais pas s'il a mis son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Il a pris la voiture a la gare.", "a", "à", "corr_a_preposition"),   # « pris » : aussi passé simple
    ("Je n'est pas fini mon travail.", "est", "ai", "corr_je_ai"),
    ("Je n'est rien fait.", "est", "ai", "corr_je_ai"),
    ("Je n'est pas dormi de la nuit.", "est", "ai", "corr_je_ai"),
    ("J'est pas fini mon travail.", "est", "ai", "corr_je_ai"),
    ("J'est allé au marché.", "J'est", "Je suis", "corr_je_ai"),
    ("Je n'est pas allé à l'école.", "n'est", "ne suis", "corr_je_ai"),
    ("Je m'est trompé.", "m'est", "me suis", "corr_je_ai"),
    ("Ils se sont trompé de chemin hier soir.", "trompé", "trompés", "corr_ppas_pronominal_sujet"),
    ("Elle s'est trompé de porte.", "trompé", "trompée", "corr_ppas_pronominal_sujet"),
    # Relecture : « que », « où », « qui » après un verbe (« dit », « note » sont aussi des noms), question.
    ("Il dit qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Il annonce qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Il écrit qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Elle note qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Il remarque qu'elle a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Il demande qu'on mette les fleurs a la fenêtre.", "a", "à", "corr_a_preposition"),
    ("Il ne dit pas qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Je crois qu'il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Je sais où il a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Je sais qui a laissé son sac a la gare.", "a", "à", "corr_a_preposition"),
    ("Qui a laissé son sac a la gare ?", "a", "à", "corr_a_preposition"),
    # Relecture : « ai » sûr sans complément d'objet (« eu », « pu »), avant un infinitif, devant « envie ».
    ("J'est eu peur.", "est", "ai", "corr_je_ai"),
    ("J'est eu de la chance.", "est", "ai", "corr_je_ai"),
    ("J'est pu venir.", "est", "ai", "corr_je_ai"),
    ("J'est dû partir.", "est", "ai", "corr_je_ai"),
    ("J'est voulu t'appeler.", "est", "ai", "corr_je_ai"),
    ("J'est envie de partir.", "est", "ai", "corr_je_ai"),
    ("Je n'en est pas envie.", "est", "ai", "corr_je_ai"),
    ("J'est été malade.", "est", "ai", "corr_je_ai"),
    ("Je t'est revenu.", "t'est", "te suis", "corr_je_ai"),
]


@pytest.mark.parametrize("text, word, replacement, rule", FIXES)
def test_rule_fixes(lex, text, word, replacement, rule):
    got = [(text[f.start:f.end], f.replacement, f.rule) for f in fautes(text, lex) if f.replacement is not None]
    assert (word, replacement, rule) in got


UNTOUCHED = [
    # Complément d'objet placé avant : le participe s'accorde avec lui, pas avec le sujet.
    "Les questions qu'ils se sont posées.",
    "Les libertés qu'elle s'est permises.",
    "La pommade qu'il a voulu me passer.",
    "Les efforts qu'il a fallu ont été grands.",
    # Complément d'objet après le participe.
    "Elles se sont lavé les mains.",
    "Elle s'est rendu compte de son erreur.",
    # Subjonctif imparfait, « si bien que », « si » interrogatif.
    "Et bien qu'il commençât en retard, il termina en avance.",
    "Il travaille si bien que tout le monde l'admire.",
    "Je me demande si tu pourrais venir.",
    # « vous » complément, groupe nominal qui n'est pas le sujet.
    "Cela vous dégoûtait de continuer.",
    "Que veut-il ? Vous parler du boulot.",
    "La maison des voisins brûle.",
    "Les valeurs limites ne peuvent pas être réglées.",   # « limites » adjectif, pas « limitent »
    # « a » verbe, « à » préposition, « peut » verbe, « ou » conjonction.
    "Je pense que le chat a la grippe.",
    "Il voit le chat qui a la queue coupée.",
    "Un moteur à cylindrée fixe.",
    "Le chat peut attendre.",
    "Il fait ce qu'il peut.",
    "Je l'ai vu il y a peu.",
    "Tu viens ou tu restes ?",
    "Je sais bien que tu reviendras.",                    # « bien » adverbe : pas « bien que » + subjonctif
    "Il travaille moins que tu crois.",
    "Je ne sais pas, ou je ne veux pas savoir.",
    "Il habite près de la gare.",
    # Divers.
    "Quelles grandes idiotes !",
    "Les pronoms « je » et « tu ».",
    "Ni lui ni elle ne viendront.",
    "Elle ne mange ni ne boit.",
    "Ils nous ont dit la vérité.",
    "Il a pris son manteau.",
    "Nous nous sommes téléphoné.",
    "Sa marche est lente.",
    "La SA ne joue plus aucun rôle.",
    "Ces gens la voient souvent.",
    "Je leur montre le chemin.",
    "Cette place est en vacance.",
    "Le chef des employés qui décide est absent.",
    "Nous nous sommes lavé les mains.",
    "Fait-il beau ?",
    "Les livres que tu m'as prêtés sont super.",
    "Elle est la meilleure.",
    "Est-ce la bonne réponse ?",
    "Il a fait une erreur.",
    "Il est là maman.",
    "Je vais fatigué au travail.",
    "Le manger a la saveur du miel.",
    "Il ou elle ne conçoivent même plus.",
    "Là commence la forêt.",
    "Il ne reste rien de changé.",
    "Il s'est un peu calmé.",
    "Il s'est en effet trompé.",
    "Mais s'est-il trompé ?",
    "Les petits pois sont bons.",
    "Est-ce que je viens ?",
    "Un moteur à cylindrée fixe.",
    "La tâche à effectuer, il s'en moquait.",
    "La maison à colombages.",
    "Il raconte en contant des histoires.",
    "Il est, contant fleurette, reparti.",
    "Il tord le fer.",
    # « a » verbe après un sujet nominal, une relative, une coordination, un participe (rapport de la 0.1.15).
    "Le chien qui aboie a peur du noir.",
    "Ce que tu dis a la valeur d'un aveu.",
    "La porte a la poignée cassée.",
    "Il mange et a la grippe.",
    "Le livre posé sur la table a la couverture rouge.",
    "Le train a 21 heures de retard.",
    "Son frère a la grippe.",
    "Il y a la mer.",
    "La ferme a la toiture refaite.",
    "Le chien qui mange un os a la queue coupée.",
    "Celui qui a le ballon a la priorité.",
    "Celui qui a faim a la priorité.",
    "L'homme dont elle aime le fils a la grippe.",
    "Le livre que tu lis le soir a la couverture rouge.",
    "Ce que tu dis le matin a la valeur d'un aveu.",
    "La maison où il passe ses vacances a la forme d'un bateau.",
    "Le livre écrit la nuit a la couverture rouge.",
    "Mon oncle porte un chapeau et a la barbe.",
    "Le soleil brille la mer a la couleur de l'encre.",
    "Quand il pleut la rivière a la couleur du café.",
    "Ce qui compte, c'est que celui qui a le ballon a la priorité.",
    # « je » + auxiliaire sans choix sûr entre « ai » et « suis » ; pronominal sans accord.
    "Je n'est pas content.",
    "J'est fatigué.",
    "J'est passé te voir.",
    "Je n'est pas fatigué ce soir.",
    "Je vous est obligé.",
    "J'est entré la tête haute.",
    # Relecture : complément de temps ou de prix, pas d'objet ; « l' » devant « suis » : « le » ou « la » ?
    "Je n'est pas occupé cet après-midi.",
    "Je n'est pas retenu toute la journée.",
    "Je n'est pas admis cette session.",
    "Je n'est pas inscrit cette saison.",
    "Je n'est pas vacciné cette saison.",
    "Je n'est pas pris cet après-midi.",
    "Je n'est pas occupé toute la semaine.",
    "Je n'est pas occupé cette quinzaine.",
    "Je n'est pas convoqué cette session.",
    "Je n'est pas payé ce trimestre.",
    "Je n'est pas payé un centime.",
    "Je me l'est demandé.",
    "Je me l'est acheté.",
    "Je ne me l'est jamais pardonné.",
    "Je me l'est procuré hier.",
    # « Je suis réussi », « Je suis compris », « Je suis dû pour des vacances » : rien ne l'exclut, rien d'office.
    "J'est réussi.",
    "J'est pleuré.",
    "J'est répondu.",
    "J'est fait de mon mieux.",
    "J'est dit non.",
    "J'est compris.",
    "J'est oublié.",
    "J'est dû pour des vacances.",
    "Il veut que moi j'aie peur.",             # subjonctif
    "Quoique j'aie mangé une pomme, j'ai faim.",
    # Relecture : préposition ou conjonction prise pour le verbe d'un sujet nominal.
    "La maison entre les arbres a la porte bleue.",
    "L'espace entre les dents a la largeur d'un cheveu.",
    "La frontière entre les pays a la forme d'une ligne droite.",
    "Le tableau entre les fenêtres a la couleur du ciel.",
    "Le village entre les montagnes a la forme d'un cirque.",
    "Le passage entre les immeubles a la largeur d'une voiture.",
    "La rivière entre les collines a la couleur du café.",
    "La différence entre les jumeaux a la taille d'un détail.",
    "La distance entre les villes a la valeur d'une journée de marche.",
    "Le duel entre les candidats a la vedette ce soir.",
    "Le mur entre les jardins a la hauteur d'un homme.",
    "La cloison entre les bureaux a la couleur du sable.",
    "Le directeur puis le maire a la parole.",
    "Le vote contre la loi a la majorité.",
    # « que », « où », « qui » relatifs, même après un mot qui est aussi un verbe (« note », « porte »).
    "La note que tu lis le soir a la valeur d'un aveu.",
    "La porte où il range ses outils a la poignée cassée.",
    "Qui a le ballon a la priorité.",
    "Ils se sont parlé.",
    "Ils se sont servi de l'eau.",
]


@pytest.mark.parametrize("text", UNTOUCHED)
def test_rules_leave_correct_text_alone(lex, text):
    assert [(text[f.start:f.end], f.replacement, f.rule) for f in fautes(text, lex) if f.replacement is not None] == []


def test_veto_drops_a_wrong_grammalecte_fix(lex):
    """« Ils se sont téléphoné » est juste : la règle écarte « téléphonés » que proposerait Grammalecte."""
    text = "Ils se sont téléphoné hier soir."
    vetoes = [f for f in fautes(text, lex) if f.replacement is None]
    assert [(text[f.start:f.end], f.rule) for f in vetoes] == [("téléphoné", "corr_ppas_pronominal_indirect")]


@pytest.mark.parametrize("text, zones", [
    ("Je pense que le chat a la grippe.", ["a"]),               # LanguageTool proposait « à »
    ("Paul a la clé.", ["a"]),
    ("Ni lui ni elle ne viendront.", ["elle", "viendront"]),   # les deux moteurs voulaient « elles »
    ("Le voyage a la mer était long.", []),                     # « à la mer » : « était » est le verbe
])
def test_vetoes_protect_correct_zones(lex, text, zones):
    from correcteur.engines.regles import check

    assert [text[f.start:f.end] for f in check(text, lex, strict=True) if f.replacement is None] == zones


def test_tokenize_splits_elisions_but_keeps_dictionary_words(lex):
    from correcteur.engines.regles import tokenize

    words = [w.text for w in tokenize("Aujourd'hui, qu'il s'est levé jusqu'à midi.", lex.known)]
    assert words == ["Aujourd'hui", "qu'", "il", "s'", "est", "levé", "jusqu'", "à", "midi"]


def test_participle_like_reads_simple_past_as_participle(lex):
    from correcteur.engines.regles import participle_like

    assert participle_like("assit", lex)[0] in ("asseoir", "assoir")
    assert participle_like("mit", lex)[0] == "mettre"
    assert participle_like("fit", lex) is None  # « fit » ne se prononce pas « fait »


def test_a_failing_rule_never_breaks_the_check(lex, monkeypatch):
    from correcteur.engines import regles

    def broken(text, words, lex):
        raise RuntimeError("règle en défaut")
        yield

    monkeypatch.setattr(regles, "RULES", (broken, regles.regle_ce_se))
    text = "Il ce lave les mains."
    assert [f.replacement for f in regles.check(text, lex)] == ["se"]
    with pytest.raises(RuntimeError):
        regles.check(text, lex, strict=True)
