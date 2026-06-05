-- WhatsApp realtime: notify subscribers when a message row is inserted.
-- Idempotent: safe to run multiple times.
--
-- The backend holds a dedicated asyncpg connection LISTENing on 'whatsapp_events'
-- and fans each notification out to GraphQL subscription clients. The trigger fires
-- for BOTH inbound (webhook) and outbound (send mutation) inserts; clients dedupe
-- by wa_message_id / id.

CREATE OR REPLACE FUNCTION app.notify_whatsapp_message() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify(
    'whatsapp_events',
    json_build_object(
      'type', 'message',
      'id', NEW.id,
      'conversation_id', NEW.conversation_id,
      'contact_id', NEW.contact_id,
      'direction', NEW.direction
    )::text
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_whatsapp_message ON app.messages;
CREATE TRIGGER trg_notify_whatsapp_message
AFTER INSERT ON app.messages
FOR EACH ROW EXECUTE FUNCTION app.notify_whatsapp_message();
