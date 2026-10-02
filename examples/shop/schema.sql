-- Shop: users and their orders.
CREATE TYPE order_status AS ENUM ('new', 'paid', 'shipped');

CREATE TABLE users (
    id      serial PRIMARY KEY,
    email   varchar(40) NOT NULL UNIQUE,
    country char(2) NOT NULL,
    age     int CHECK (age >= 18)
);

CREATE TABLE orders (
    id      int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id int NOT NULL REFERENCES users(id),
    status  order_status NOT NULL,
    amount  numeric(10,2) NOT NULL CHECK (amount > 0),
    meta    jsonb,
    tags    text[] NOT NULL
);
