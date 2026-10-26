"""Small, conservative guards: a payment question is not permission to charge."""
import re
import unicodedata


def plain(text):
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))))


def acknowledgment(text):
    value = plain(text)
    if re.fullmatch(r"(?:hola|buenos dias|buenas tardes|buenas noches|buenas|hola buenos dias|hola buenas tardes|hola buenas noches)", value):
        return "¡Hola! 😊 ¿En qué podemos ayudarte?"
    if re.fullmatch(r"(?:muchas |mil )?gracias(?: por todo)?|(?:ok |muchas )?gracias igualmente", value):
        return "¡De nada! 😊 Estamos para ayudarte."
    return None


def payment_question(text):
    value = plain(text)
    return bool(re.search(r"\b(?:efectivo|recepcion|presencial|transferencia|clabe|cuenta bancaria|numero de cuenta|formas de pago|metodos de pago)\b", value)
                or re.search(r"\b(?:pagar (?:ahi|alli|alla|al llegar|cuando vaya)|como (?:pago|puedo pagar)|pudiera pagar ahi)\b", value))


def in_person_choice(text):
    value = plain(text)
    return bool(re.search(r"\b(?:prefiero|quiero|voy a|pagare|mejor|elijo)\b.{0,45}\b(?:recepcion|efectivo|presencial|pagar ahi|pagar alli|pagar al llegar)\b", value))


def reservation_question(text):
    value = plain(text)
    return not acknowledgment(text) and bool(re.search(
        r"\b(?:reserva[s]?|reservar|apartar|apartado|mi lugar|mi bici|mis clases|mi clase|tengo clase|tengo lugar|que dia|que hora|cuando (?:voy|tengo|es mi)|horario|asistir|quiero pagar)\b", value))


def fallback(text):
    return acknowledgment(text) or "No pude completar tu consulta con seguridad. ¿Puedes indicarme qué necesitas revisar?"


def payment_options(offer_in_person):
    text = ("Puedes pagar con tarjeta o por transferencia desde el mismo enlace de Mercado Pago. "
            "Ahí encontrarás las instrucciones y te avisaremos por aquí cuando se acredite.")
    if offer_in_person:
        text += " También puedes registrarte y pagar en recepción con nuestro equipo."
    return text
