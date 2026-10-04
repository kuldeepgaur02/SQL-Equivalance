-- The problem statement's example: courses with their "rich" departments.
CREATE TABLE departments (
    dept_id int PRIMARY KEY,
    budget  int NOT NULL
);
CREATE TABLE courses (
    cid     varchar(10) PRIMARY KEY,
    dept_id int NOT NULL REFERENCES departments(dept_id)
);
