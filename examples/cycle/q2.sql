SELECT d.name, count(e.mentor_id) FROM departments d JOIN employees e ON e.dept_id = d.id
JOIN a ON a.id = e.id GROUP BY d.name
