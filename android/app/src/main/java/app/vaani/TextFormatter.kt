package app.vaani

import java.util.Locale

/**
 * Smart formatting and punctuation rules for dictated speech.
 * Formats contractions, capitalizes sentence starts, handles pronoun "I", and manages punctuation.
 */
object TextFormatter {

    private val CONTRACTIONS = mapOf(
        "im" to "I'm",
        "i'm" to "I'm",
        "ill" to "I'll",
        "i'll" to "I'll",
        "ive" to "I've",
        "i've" to "I've",
        "id" to "I'd",
        "i'd" to "I'd",
        "dont" to "don't",
        "cant" to "can't",
        "wont" to "won't",
        "didnt" to "didn't",
        "isnt" to "isn't",
        "arent" to "aren't",
        "wasnt" to "wasn't",
        "werent" to "weren't",
        "thats" to "that's",
        "whats" to "what's",
        "lets" to "let's",
        "theres" to "there's",
        "heres" to "here's",
        "youre" to "you're",
        "you're" to "you're",
        "theyre" to "they're",
        "weve" to "we've",
        "theyve" to "they've",
        "couldnt" to "couldn't",
        "shouldnt" to "shouldn't",
        "wouldnt" to "wouldn't",
        "hasnt" to "hasn't",
        "havent" to "haven't",
        "hadnt" to "hadn't"
    )

    /** Indian personal names, brands, and place names frequently mis-heard by generic ASR models. */
    private val INDIAN_ENTITIES = mapOf(
        "vaani" to "Vaani",
        "wispr" to "Wispr",
        "whsiepr" to "Wispr",
        "sachin" to "Sachin",
        "satjian" to "Sachin",
        "rajshri" to "Raj Shamani",
        "rajshrimani" to "Raj Shamani",
        "nikhil" to "Nikhil",
        "nikhilkamath" to "Nikhil Kamath",
        "kamat" to "Kamath",
        "bhubaneshwar" to "Bhubaneswar",
        "bhubneshwar" to "Bhubaneswar",
        "bengaluru" to "Bengaluru",
        "mumbai" to "Mumbai",
        "dilli" to "Delhi",
        "hydrabad" to "Hyderabad",
        "hydrabad" to "Hyderabad",
        "chennai" to "Chennai"
    )

    private val TRAILING_PUNCT = Regex("[.,!?;:]+\$")

    private val QUESTION_STARTERS = setOf(
        "what", "why", "how", "who", "whom", "whose", "where", "when", "which",
        "can", "could", "would", "should", "is", "are", "am", "do", "does", "did",
        "will", "wont", "have", "has", "had", "haven", "hasn", "aren", "isn",
        "shall", "may", "might", "must", "need", "dare"
    )

    fun format(text: String, lang: String, isFinal: Boolean = false): String {
        if (text.isBlank()) return ""
        val clean = text.replace(Regex("\\s+"), " ").trim()
        if (lang != "en") {
            return clean
        }

        // English formatting
        val words = clean.split(' ').toMutableList()
        for (i in words.indices) {
            val raw = words[i]
            // Strip trailing punctuation for lookup, restore after substitution
            val trailingPunct = TRAILING_PUNCT.find(raw)?.value ?: ""
            val stripped = if (trailingPunct.isNotEmpty()) raw.dropLast(trailingPunct.length) else raw
            val lower = stripped.lowercase(Locale.ROOT)

            when {
                lower == "i" -> words[i] = "I$trailingPunct"
                INDIAN_ENTITIES.containsKey(lower) -> words[i] = INDIAN_ENTITIES[lower]!! + trailingPunct
                CONTRACTIONS.containsKey(lower) -> words[i] = CONTRACTIONS[lower]!! + trailingPunct
            }
        }

        val joined = words.joinToString(" ")

        // Capitalize after start of string or after sentence ending punctuation (. ? !)
        val sb = StringBuilder()
        var capitalizeNext = true
        for (ch in joined) {
            if (capitalizeNext && ch.isLetter()) {
                sb.append(ch.titlecaseChar())
                capitalizeNext = false
            } else {
                sb.append(ch)
                if (ch == '.' || ch == '?' || ch == '!') {
                    capitalizeNext = true
                }
            }
        }
        var result = sb.toString()

        if (isFinal && result.isNotEmpty()) {
            val lastChar = result.last()
            if (lastChar !in setOf('.', '?', '!', ',', ';', ':')) {
                val firstWord = result.split(' ').first().lowercase(Locale.ROOT).substringBefore('\'')
                if (firstWord in QUESTION_STARTERS) {
                    result += "?"
                }
            }
        }

        return result
    }
}
