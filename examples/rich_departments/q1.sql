-- filter inside the LEFT JOIN: every course is kept
SELECT c.cid, d.dept_id
FROM courses c LEFT JOIN departments d
  ON c.dept_id = d.dept_id AND d.budget > 100000;
