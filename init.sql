-- Initialize database schema for schedparse application

-- Create eblans table for storing lecturer info and ratings
CREATE TABLE IF NOT EXISTS eblans (
    eblan_id INTEGER PRIMARY KEY,
    eblan_fio VARCHAR(255),
    eblan_img VARCHAR(500),
    eblan_img_approved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create comments table
CREATE TABLE IF NOT EXISTS eblan_comments (
    id SERIAL PRIMARY KEY,
    eblan_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    comment TEXT,
    features TEXT[], -- Array of feature strings
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address INET,
    FOREIGN KEY (eblan_id) REFERENCES eblans(eblan_id) ON DELETE CASCADE
);

-- Create lecture images table
CREATE TABLE IF NOT EXISTS lecture_images (
    id SERIAL PRIMARY KEY,
    eblan_id INTEGER,
    lect_string VARCHAR(100) NOT NULL,
    image_path VARCHAR(500) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address INET,
    approved BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (eblan_id) REFERENCES eblans(eblan_id) ON DELETE SET NULL
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_eblan_comments_eblan_id ON eblan_comments(eblan_id);
CREATE INDEX IF NOT EXISTS idx_eblan_comments_created_at ON eblan_comments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lecture_images_eblan_lect ON lecture_images(eblan_id, lect_string);
CREATE INDEX IF NOT EXISTS idx_lecture_images_lect_string ON lecture_images(lect_string);
CREATE INDEX IF NOT EXISTS idx_lecture_images_created_at ON lecture_images(created_at DESC);

-- Insert some sample data for testing (optional)
-- INSERT INTO eblans (eblan_id, eblan_fio) VALUES 
-- (12345, 'Иванов Иван Иванович'),
-- (67890, 'Петров Петр Петрович')
-- ON CONFLICT (eblan_id) DO NOTHING;