SELECT o.id, u.email
FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.amount > 100 AND o.status = 'paid' AND o.meta->>'channel' = 'web';
